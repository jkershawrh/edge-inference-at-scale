"""Shared pytest fixtures for Edge Inference at Scale test suite.

All fixtures use mocks — no external services (BitNet, Redis, ChromaDB)
are required to run the test suite.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Sample data factories
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_sms_message() -> Dict[str, Any]:
    """A representative inbound SMS message dict."""
    return {
        "id": str(uuid.uuid4()),
        "sender": "+15551234567",
        "receiver": "+15559876543",
        "content": "What sessions are about edge computing?",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "priority": "normal",
        "metadata": None,
    }


@pytest.fixture
def sample_emergency_sms() -> Dict[str, Any]:
    """An SMS message with emergency keywords."""
    return {
        "id": str(uuid.uuid4()),
        "sender": "+15551234567",
        "receiver": "+15559876543",
        "content": "help fire emergency in hall B",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "priority": "normal",
        "metadata": None,
    }


@pytest.fixture
def sample_llm_request() -> Dict[str, Any]:
    """A representative LLM inference request payload."""
    return {
        "prompt": "What sessions are about edge computing?",
        "context": "Session: Edge Computing Workshop - Room 301, 2:00 PM",
        "max_length": 160,
        "temperature": 0.7,
        "model": None,
    }


@pytest.fixture
def sample_chat_completion_response() -> Dict[str, Any]:
    """A mock OpenAI-compatible chat completion response from BitNet."""
    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "bitnet-2b4t",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Edge Computing Workshop is in Room 301 at 2 PM today.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 45,
            "completion_tokens": 15,
            "total_tokens": 60,
        },
    }


@pytest.fixture
def sample_long_chat_completion_response() -> Dict[str, Any]:
    """A chat completion response whose content exceeds 160 characters."""
    long_content = (
        "The Edge Computing Workshop covers deploying inference models at the "
        "edge using BitNet 1.58-bit ternary quantization for CPU-only operation "
        "without GPU acceleration, enabling deployment on resource-constrained "
        "hardware in disconnected environments."
    )
    assert len(long_content) > 160
    return {
        "id": "chatcmpl-test456",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "bitnet-2b4t",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": long_content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 45,
            "completion_tokens": 60,
            "total_tokens": 105,
        },
    }


# ---------------------------------------------------------------------------
# Mock BitNet server (httpx mock)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bitnet_server(sample_chat_completion_response):
    """An httpx-compatible mock that returns chat completion responses.

    Usage in tests::

        async with httpx.AsyncClient(transport=mock_bitnet_server) as client:
            resp = await client.post("/v1/chat/completions", json=payload)
    """
    import httpx

    async def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/v1/chat/completions" and request.method == "POST":
            return httpx.Response(
                status_code=200,
                json=sample_chat_completion_response,
            )

        if path == "/v1/models" and request.method == "GET":
            return httpx.Response(
                status_code=200,
                json={
                    "data": [
                        {
                            "id": "bitnet-2b4t",
                            "object": "model",
                            "owned_by": "microsoft",
                        }
                    ]
                },
            )

        # Health-ish probe
        if path == "/health":
            return httpx.Response(status_code=200, json={"status": "ok"})

        return httpx.Response(status_code=404, json={"error": "not found"})

    transport = httpx.MockTransport(_handler)
    return transport


@pytest.fixture
def mock_bitnet_server_unavailable():
    """An httpx transport that always raises ConnectError."""
    import httpx

    async def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    return httpx.MockTransport(_handler)


# ---------------------------------------------------------------------------
# Mock Redis
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    """A mock Redis client with common async operations stubbed."""
    redis = MagicMock()
    redis.ping = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.lpush = AsyncMock(return_value=1)
    redis.rpop = AsyncMock(return_value=None)
    redis.llen = AsyncMock(return_value=0)
    redis.close = AsyncMock()
    return redis


# ---------------------------------------------------------------------------
# Mock ChromaDB
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_chromadb():
    """A mock ChromaDB client and collection."""
    collection = MagicMock()
    collection.add = MagicMock()
    collection.query = MagicMock(
        return_value={
            "documents": [
                ["Edge Computing Workshop - Room 301, 2:00 PM"],
            ],
            "distances": [[0.25]],
            "metadatas": [[{"source": "summit_schedule.txt"}]],
            "ids": [["doc-001"]],
        }
    )
    collection.count = MagicMock(return_value=10)

    client = MagicMock()
    client.get_or_create_collection = MagicMock(return_value=collection)

    return client
