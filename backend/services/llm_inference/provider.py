"""Hardware-neutral configuration for OpenAI-compatible inference servers.

The edge runtime deliberately targets the small OpenAI-compatible HTTP surface
implemented by llama.cpp, vLLM, TGI, and several vendor runtimes.  Keeping the
provider identity separate from the protocol lets the same application image
move between BitNet, conventional GGUF models, and accelerator-specific
servers without changing the message pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

import httpx

from backend.shared.config import Settings


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved inference provider settings."""

    name: str
    base_url: str
    model: str
    model_memory_mb: float


def resolve_provider_config(
    environ: Mapping[str, str] | None = None,
    app_settings: Settings | None = None,
) -> ProviderConfig:
    """Resolve generic settings while preserving the legacy BitNet variables."""
    env = environ if environ is not None else os.environ
    configured = app_settings or Settings()

    name = env.get("LLM_PROVIDER", configured.llm_provider)
    base_url = env.get(
        "LLM_BASE_URL",
        env.get("BITNET_SERVER_URL", configured.bitnet_server_url),
    )
    model = env.get(
        "LLM_MODEL",
        env.get("MODEL_NAME", configured.default_model),
    )

    default_memory = 410.0 if name.lower() == "bitnet" else 0.0
    memory_text = env.get("MODEL_MEMORY_MB", str(default_memory))
    try:
        model_memory_mb = max(0.0, float(memory_text))
    except ValueError:
        model_memory_mb = default_memory

    return ProviderConfig(
        name=name,
        base_url=base_url.rstrip("/"),
        model=model,
        model_memory_mb=model_memory_mb,
    )


class OpenAICompatibleProvider:
    """Minimal adapter shared by local and accelerator-backed runtimes."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    async def available(self, client: httpx.AsyncClient) -> bool:
        """Return whether the provider exposes an accessible model endpoint."""
        try:
            response = await client.get("/v1/models")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def complete(
        self, client: httpx.AsyncClient, payload: dict
    ) -> httpx.Response:
        """Submit an OpenAI-compatible chat-completion request."""
        return await client.post("/v1/chat/completions", json=payload)
