"""LLM Inference service for Edge Inference at Scale.

Wraps a BitNet llama-server's OpenAI-compatible API to provide
inference for the Summit Connect SMS assistant.
"""

from __future__ import annotations

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

logger = logging.getLogger("llm-inference")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BITNET_SERVER_URL = os.environ.get("BITNET_SERVER_URL", settings.bitnet_server_url)
MODEL_NAME = os.environ.get("MODEL_NAME", settings.model_name)

SYSTEM_PROMPT = (
    "You are the Summit Connect Assistant. You help conference attendees "
    "find information about sessions, speakers, schedules, venues, and "
    "local area activities. Keep your responses concise — under 160 "
    "characters for SMS delivery. Be helpful and direct."
)

# Estimated memory footprint for BitNet 2B4T (1.58-bit weights)
MODEL_MEMORY_MB = 410.0

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
}

# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------
_http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the httpx client lifecycle."""
    global _http_client
    _http_client = httpx.AsyncClient(
        base_url=BITNET_SERVER_URL,
        timeout=httpx.Timeout(settings.llm_request_timeout_seconds, connect=5.0),
    )
    logger.info(
        "LLM Inference service started — BitNet server: %s, model: %s",
        BITNET_SERVER_URL,
        MODEL_NAME,
    )
    yield
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _bitnet_available() -> bool:
    """Return True if the BitNet server responds to a health-ish probe."""
    try:
        resp = await _http_client.get("/v1/models")
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
@app.get("/health", response_model=ServiceHealth)
async def health():
    """Check BitNet server connectivity and return service health."""
    bitnet_ok = await _bitnet_available()
    status = "healthy" if bitnet_ok else "degraded"
    details: Dict[str, Any] = {
        "bitnet_server_url": BITNET_SERVER_URL,
        "bitnet_server_reachable": bitnet_ok,
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
    """Run inference via the BitNet llama-server.

    Builds an OpenAI-compatible chat completion request, sends it to the
    BitNet server, and returns an LLMResponse.
    """
    _stats["requests_total"] += 1
    start = time.perf_counter()

    # Build the messages list -------------------------------------------------
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if request.context:
        messages.append(
            {
                "role": "user",
                "content": f"Context information:\n{request.context}",
            }
        )

    messages.append({"role": "user", "content": request.prompt})

    # Build the request payload -----------------------------------------------
    model = request.model or MODEL_NAME
    payload = {
        "model": model,
        "messages": messages,
        "temperature": request.temperature,
        "max_tokens": request.max_length,
    }

    # Call the BitNet server ---------------------------------------------------
    try:
        resp = await _http_client.post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
    except httpx.ConnectError:
        _stats["requests_failed"] += 1
        raise HTTPException(
            status_code=502,
            detail=f"Cannot connect to BitNet server at {BITNET_SERVER_URL}",
        )
    except httpx.TimeoutException:
        _stats["requests_failed"] += 1
        raise HTTPException(
            status_code=502,
            detail="BitNet server request timed out",
        )
    except httpx.HTTPStatusError as exc:
        _stats["requests_failed"] += 1
        raise HTTPException(
            status_code=502,
            detail=f"BitNet server returned {exc.response.status_code}: {exc.response.text}",
        )
    except Exception as exc:
        _stats["requests_failed"] += 1
        logger.exception("Unexpected error calling BitNet server")
        raise HTTPException(
            status_code=502,
            detail=f"BitNet server error: {exc}",
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
    """Pass-through proxy to the BitNet server's chat completions endpoint."""
    try:
        resp = await _http_client.post("/v1/chat/completions", json=request)
        resp.raise_for_status()
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot connect to BitNet server at {BITNET_SERVER_URL}",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=502,
            detail="BitNet server request timed out",
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
    """Pass-through proxy to the BitNet server's models endpoint."""
    try:
        resp = await _http_client.get("/v1/models")
        resp.raise_for_status()
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot connect to BitNet server at {BITNET_SERVER_URL}",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=502,
            detail="BitNet server request timed out",
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
        "bitnet_server_url": BITNET_SERVER_URL,
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
