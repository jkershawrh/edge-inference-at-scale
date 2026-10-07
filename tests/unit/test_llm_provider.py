"""Tests for the hardware-neutral inference provider boundary."""

import httpx
import pytest

from backend.services.llm_inference.provider import (
    OpenAICompatibleProvider,
    ProviderConfig,
    resolve_provider_config,
)
from backend.shared.config import Settings


def test_generic_provider_variables_override_legacy_bitnet_names():
    config = resolve_provider_config(
        {
            "LLM_PROVIDER": "llama-cpp",
            "LLM_BASE_URL": "http://models:9000/",
            "LLM_MODEL": "ministral-3b-q4",
            "MODEL_MEMORY_MB": "2450",
            "BITNET_SERVER_URL": "http://legacy:8080",
            "MODEL_NAME": "legacy-bitnet",
        },
        Settings(),
    )

    assert config.name == "llama-cpp"
    assert config.base_url == "http://models:9000"
    assert config.model == "ministral-3b-q4"
    assert config.model_memory_mb == 2450.0


def test_legacy_bitnet_variables_remain_supported():
    config = resolve_provider_config(
        {
            "BITNET_SERVER_URL": "http://bitnet-test:8080",
            "MODEL_NAME": "bitnet-test",
        },
        Settings(),
    )

    assert config.name == "bitnet"
    assert config.base_url == "http://bitnet-test:8080"
    assert config.model == "bitnet-test"
    assert config.model_memory_mb == 410.0


@pytest.mark.asyncio
async def test_provider_uses_openai_compatible_endpoints():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"choices": []})

    provider = OpenAICompatibleProvider(
        ProviderConfig("test", "http://provider", "model", 0.0)
    )
    async with httpx.AsyncClient(
        base_url="http://provider", transport=httpx.MockTransport(handler)
    ) as client:
        assert await provider.available(client) is True
        response = await provider.complete(client, {"model": "model"})

    assert response.status_code == 200
    assert seen == [
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
    ]


@pytest.mark.asyncio
async def test_provider_unavailable_on_transport_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    provider = OpenAICompatibleProvider(
        ProviderConfig("test", "http://provider", "model", 0.0)
    )
    async with httpx.AsyncClient(
        base_url="http://provider", transport=httpx.MockTransport(handler)
    ) as client:
        assert await provider.available(client) is False
