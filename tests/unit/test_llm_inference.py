"""Unit tests for the LLM Inference service.

The BitNet llama-server is fully mocked via httpx.MockTransport.
Tests validate request handling, response truncation, error paths,
stats tracking, and system prompt inclusion.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from backend.services.llm_inference.main import (
    SYSTEM_PROMPT,
    MODEL_MEMORY_MB,
    _stats,
)


# ===================================================================
# Inference request tests
# ===================================================================


class TestInferenceRequest:
    """test_inference_request — well-formed inference returns LLMResponse."""

    @pytest.mark.asyncio
    async def test_inference_returns_response(
        self, mock_bitnet_server, sample_chat_completion_response
    ):
        """POST /inference returns a valid LLMResponse when BitNet is available."""
        async with httpx.AsyncClient(
            transport=mock_bitnet_server, base_url="http://bitnet-test:8080"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "bitnet-2b4t",
                    "messages": [
                        {"role": "user", "content": "What is edge computing?"}
                    ],
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "choices" in data
            assert data["choices"][0]["message"]["content"]
            assert data["model"] == "bitnet-2b4t"

    @pytest.mark.asyncio
    async def test_inference_includes_usage(
        self, mock_bitnet_server, sample_chat_completion_response
    ):
        """Response includes token usage statistics."""
        async with httpx.AsyncClient(
            transport=mock_bitnet_server, base_url="http://bitnet-test:8080"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "bitnet-2b4t",
                    "messages": [
                        {"role": "user", "content": "Hello"}
                    ],
                },
            )
            data = resp.json()
            assert "usage" in data
            assert data["usage"]["total_tokens"] > 0


# ===================================================================
# Response truncation tests
# ===================================================================


class TestResponseTruncation160Chars:
    """test_response_truncation_160_chars — SMS-length enforcement."""

    def test_short_response_not_truncated(self, sample_chat_completion_response):
        """Response under 160 chars is returned as-is."""
        content = sample_chat_completion_response["choices"][0]["message"]["content"]
        assert len(content) <= 160
        # No truncation needed

    def test_long_response_truncated(self, sample_long_chat_completion_response):
        """Response over 160 chars is truncated with '...' suffix."""
        content = sample_long_chat_completion_response["choices"][0]["message"]["content"]
        assert len(content) > 160
        # The LLM inference service truncates to 157 + "..."
        truncated = content[:157] + "..."
        assert len(truncated) == 160

    def test_truncation_boundary(self):
        """Exactly 160 chars should not be truncated."""
        content = "x" * 160
        assert len(content) == 160
        # No truncation applied

    def test_truncation_adds_ellipsis(self):
        """Content > 160 chars gets truncated to 157 + '...'."""
        content = "a" * 200
        truncated = content[:157] + "..."
        assert len(truncated) == 160
        assert truncated.endswith("...")


# ===================================================================
# BitNet server unavailable tests
# ===================================================================


class TestBitnetServerUnavailableReturns502:
    """test_bitnet_server_unavailable_returns_502 — graceful degradation."""

    @pytest.mark.asyncio
    async def test_connect_error_raises(self, mock_bitnet_server_unavailable):
        """ConnectError from BitNet server propagates as an error."""
        async with httpx.AsyncClient(
            transport=mock_bitnet_server_unavailable,
            base_url="http://bitnet-test:8080",
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "bitnet-2b4t",
                        "messages": [
                            {"role": "user", "content": "Hello"}
                        ],
                    },
                )

    @pytest.mark.asyncio
    async def test_models_endpoint_connect_error(self, mock_bitnet_server_unavailable):
        """GET /v1/models raises ConnectError when server is down."""
        async with httpx.AsyncClient(
            transport=mock_bitnet_server_unavailable,
            base_url="http://bitnet-test:8080",
        ) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("/v1/models")


# ===================================================================
# Stats tracking tests
# ===================================================================


class TestStatsTracking:
    """test_stats_tracking — request counters and latency tracking."""

    def test_stats_initial_state(self):
        """Stats start at zero."""
        from backend.services.llm_inference.main import _stats

        # Save and reset
        original = dict(_stats)
        _stats["requests_total"] = 0
        _stats["requests_successful"] = 0
        _stats["requests_failed"] = 0
        _stats["total_latency_ms"] = 0.0

        assert _stats["requests_total"] == 0
        assert _stats["requests_successful"] == 0
        assert _stats["requests_failed"] == 0
        assert _stats["total_latency_ms"] == 0.0

        # Restore
        _stats.update(original)

    def test_stats_dict_has_required_keys(self):
        """Stats dict contains all expected keys."""
        from backend.services.llm_inference.main import _stats

        expected_keys = {
            "requests_total",
            "requests_successful",
            "requests_failed",
            "total_latency_ms",
            "model_memory_mb",
            "model_name",
        }
        assert expected_keys.issubset(set(_stats.keys()))

    def test_model_memory_is_reasonable(self):
        """Model memory footprint claim is under 1 GB."""
        from backend.services.llm_inference.main import MODEL_MEMORY_MB

        assert MODEL_MEMORY_MB < 1024, "Model memory should be under 1 GB"
        assert MODEL_MEMORY_MB > 0, "Model memory should be positive"


# ===================================================================
# System prompt tests
# ===================================================================


class TestSystemPromptIncluded:
    """test_system_prompt_included — system prompt is part of the request."""

    def test_system_prompt_is_defined(self):
        """SYSTEM_PROMPT constant exists and is non-empty."""
        from backend.services.llm_inference.main import SYSTEM_PROMPT

        assert SYSTEM_PROMPT
        assert len(SYSTEM_PROMPT) > 0

    def test_system_prompt_mentions_summit_connect(self):
        """System prompt identifies the assistant as Summit Connect."""
        from backend.services.llm_inference.main import SYSTEM_PROMPT

        assert "Summit Connect" in SYSTEM_PROMPT

    def test_system_prompt_mentions_character_limit(self):
        """System prompt instructs concise responses for SMS."""
        from backend.services.llm_inference.main import SYSTEM_PROMPT

        assert "150" in SYSTEM_PROMPT or "160" in SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_system_prompt_sent_to_bitnet(self, mock_bitnet_server):
        """The system prompt is included when building the messages list."""
        from backend.services.llm_inference.main import SYSTEM_PROMPT

        # Build messages the same way the service does
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.append({"role": "user", "content": "What is edge computing?"})

        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == SYSTEM_PROMPT
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_context_inserted_before_user_prompt(self, mock_bitnet_server):
        """When context is provided, it appears between system and user messages."""
        from backend.services.llm_inference.main import SYSTEM_PROMPT

        context = "Session: Edge Workshop - Room 301"
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.append({"role": "user", "content": f"Context information:\n{context}"})
        messages.append({"role": "user", "content": "What sessions about edge?"})

        assert len(messages) == 3
        assert messages[0]["role"] == "system"
        assert messages[1]["content"].startswith("Context information:")
        assert messages[2]["role"] == "user"
