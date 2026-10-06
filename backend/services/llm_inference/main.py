"""Hardware-neutral LLM inference service for Edge Inference at Scale."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.shared.config import settings
from backend.shared.models import LLMRequest, LLMResponse, ServiceHealth
from backend.services.llm_inference.provider import (
    OpenAICompatibleProvider,
    resolve_provider_config,
)

logger = logging.getLogger("llm-inference")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROVIDER_CONFIG = resolve_provider_config(os.environ, settings)
LLM_PROVIDER = PROVIDER_CONFIG.name
LLM_BASE_URL = PROVIDER_CONFIG.base_url
MODEL_NAME = PROVIDER_CONFIG.model
PROVIDER = OpenAICompatibleProvider(PROVIDER_CONFIG)

# Backward-compatible public name for existing tests and deployments.
BITNET_SERVER_URL = LLM_BASE_URL

SYSTEM_PROMPT = (
    "You are a helpful SMS assistant for Summit Connect conference. "
    "Answer the user's question using ONLY the provided context. "
    "Give ONE short answer. Do NOT repeat the question. Do NOT repeat yourself. "
    "Maximum 150 characters. If you don't know, say 'Sorry, I don't have that info.'"
)

MODEL_MEMORY_MB = PROVIDER_CONFIG.model_memory_mb

# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------
_stats: Dict[str, Any] = {
    "requests_total": 0,
    "requests_successful": 0,
    "requests_failed": 0,
    "total_latency_ms": 0.0,
    "model_memory_mb": MODEL_MEMORY_MB,
    "model_name": MODEL_NAME,
    "provider": LLM_PROVIDER,
}

# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------
_http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the httpx client lifecycle."""
    global _http_client
    owns_client = _http_client is None
    if owns_client:
        _http_client = httpx.AsyncClient(
            base_url=LLM_BASE_URL,
            timeout=httpx.Timeout(settings.llm_request_timeout_seconds, connect=10.0),
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
                keepalive_expiry=3,
            ),
        )
    # Warm the configured provider connection.
    for attempt in range(10):
        try:
            resp = await _http_client.get("/health")
            if resp.status_code == 200:
                logger.info("BitNet connection warm (attempt %d)", attempt + 1)
                break
        except Exception:
            pass
        await asyncio.sleep(1)
    logger.info(
        "LLM Inference service started — provider: %s, server: %s, model: %s",
        LLM_PROVIDER,
        LLM_BASE_URL,
        MODEL_NAME,
    )
    yield
    if owns_client and _http_client is not None:
        await _http_client.aclose()
        _http_client = None
    logger.info("LLM Inference service stopped")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Edge Inference at Scale - LLM Inference",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _provider_available() -> bool:
    """Return True when the configured inference provider is reachable."""
    if _http_client is None:
        return False
    return await PROVIDER.available(_http_client)


# Compatibility for imports used by older integrations.
_bitnet_available = _provider_available


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
@app.get("/health", response_model=ServiceHealth)
async def health():
    """Check inference provider connectivity and return service health."""
    provider_ok = await _provider_available()
    status = "healthy" if provider_ok else "degraded"
    details: Dict[str, Any] = {
        "provider": LLM_PROVIDER,
        "llm_base_url": LLM_BASE_URL,
        "provider_reachable": provider_ok,
        # Kept during the migration window for existing dashboards.
        "bitnet_server_url": LLM_BASE_URL,
        "bitnet_server_reachable": provider_ok,
        "model_name": MODEL_NAME,
        "model_memory_mb": MODEL_MEMORY_MB,
        "requests_total": _stats["requests_total"],
    }
    return ServiceHealth(
        service_name="llm-inference",
        status=status,
        version="0.1.0",
        details=details,
    )


# ---------------------------------------------------------------------------
# POST /inference
# ---------------------------------------------------------------------------
@app.post("/inference", response_model=LLMResponse)
async def inference(request: LLMRequest):
    """Run inference through the configured OpenAI-compatible provider.

    Builds an OpenAI-compatible chat completion request, sends it to the
    configured local server, and returns an LLMResponse.
    """
    _stats["requests_total"] += 1
    start = time.perf_counter()

    # Build the messages list -------------------------------------------------
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Insert chat history (prior turns) before the current message
    if request.chat_history:
        for turn in request.chat_history:
            messages.append({"role": turn["role"], "content": turn["content"]})

    if request.context:
        messages.append(
            {
                "role": "user",
                "content": f"CONTEXT: {request.context}\n\nQUESTION: {request.prompt}\n\nANSWER:",
            }
        )
    else:
        messages.append({"role": "user", "content": request.prompt})

    # Build the request payload -----------------------------------------------
    model = request.model or MODEL_NAME
    payload = {
        "model": model,
        "messages": messages,
        "temperature": request.temperature,
        "max_tokens": request.max_length,
    }

    # Call the provider with bounded retries on transient errors. -------------
    last_exc: Exception | None = None
    for _attempt in range(3):
        try:
            resp = await PROVIDER.complete(_http_client, payload)
            resp.raise_for_status()
            break
        except httpx.TimeoutException:
            _stats["requests_failed"] += 1
            raise HTTPException(
                status_code=502,
                detail=f"{LLM_PROVIDER} inference request timed out",
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:200] if exc.response else "no body"
            logger.warning("Provider call failed (attempt %d/3, %s %s): %s", _attempt + 1, exc.response.status_code, type(exc).__name__, body)
            last_exc = exc
            if _attempt < 2:
                await asyncio.sleep(0.5)
                continue
        except (httpx.TransportError, Exception) as exc:
            logger.warning("Provider call failed (attempt %d/3, %s): %s", _attempt + 1, type(exc).__name__, exc)
            last_exc = exc
            if _attempt < 2:
                await asyncio.sleep(0.5)
                continue
    else:
        _stats["requests_failed"] += 1
        raise HTTPException(
            status_code=502,
            detail=f"{LLM_PROVIDER} provider error after 3 attempts: {type(last_exc).__name__}: {last_exc}",
        )

    # Parse response -----------------------------------------------------------
    data = resp.json()
    elapsed_ms = (time.perf_counter() - start) * 1000

    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError):
        _stats["requests_failed"] += 1
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected response structure from BitNet server: {data}",
        )

    usage = data.get("usage", {})
    tokens_used = usage.get("total_tokens", 0)

    # Truncate to 160 characters for SMS delivery
    if len(content) > 160:
        content = content[:157] + "..."

    _stats["requests_successful"] += 1
    _stats["total_latency_ms"] += elapsed_ms

    return LLMResponse(
        response=content,
        model_used=data.get("model", model),
        tokens_used=tokens_used,
        processing_time=round(elapsed_ms, 2),
        metadata={
            "finish_reason": choice.get("finish_reason"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "truncated": len(choice["message"]["content"]) > 160,
        },
    )


# ---------------------------------------------------------------------------
# POST /v1/chat/completions — passthrough
# ---------------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions_passthrough(request: dict):
    """Pass through to the configured provider's chat completions endpoint."""
    try:
        resp = await _http_client.post("/v1/chat/completions", json=request)
        resp.raise_for_status()
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot connect to {LLM_PROVIDER} provider at {LLM_BASE_URL}",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=502,
            detail=f"{LLM_PROVIDER} provider request timed out",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.text,
        )


# ---------------------------------------------------------------------------
# GET /v1/models — passthrough
# ---------------------------------------------------------------------------
@app.get("/v1/models")
async def models_passthrough():
    """Pass through to the configured provider's models endpoint."""
    try:
        resp = await _http_client.get("/v1/models")
        resp.raise_for_status()
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot connect to {LLM_PROVIDER} provider at {LLM_BASE_URL}",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=502,
            detail=f"{LLM_PROVIDER} provider request timed out",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=exc.response.text,
        )


# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------
@app.get("/stats")
async def stats():
    """Return inference statistics."""
    total = _stats["requests_total"]
    successful = _stats["requests_successful"]
    avg_latency = (
        _stats["total_latency_ms"] / successful if successful > 0 else 0.0
    )
    return {
        "requests_total": total,
        "requests_successful": successful,
        "requests_failed": _stats["requests_failed"],
        "average_latency_ms": round(avg_latency, 2),
        "total_latency_ms": round(_stats["total_latency_ms"], 2),
        "model_name": _stats["model_name"],
        "model_memory_mb": _stats["model_memory_mb"],
        "provider": _stats["provider"],
        "llm_base_url": LLM_BASE_URL,
        "bitnet_server_url": LLM_BASE_URL,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "backend.services.llm_inference.main:app",
        host="0.0.0.0",
        port=settings.llm_inference_port,
        reload=True,
    )
