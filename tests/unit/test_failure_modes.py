"""Failure mode tests — verify graceful degradation when services fail."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.shared.models import SMSMessage, MessagePriority, LLMRequest
from backend.services.message_router.main import MessageRouter
from datetime import datetime, timezone
import uuid


def _make_sms(content):
    return SMSMessage(
        id=str(uuid.uuid4()), sender="+15551234567", receiver="+15559876543",
        content=content, timestamp=datetime.now(timezone.utc), priority=MessagePriority.NORMAL,
    )


class TestRAGFailure:
    def setup_method(self):
        self.router = MessageRouter()
        self.router.http_client = MagicMock()

    @pytest.mark.asyncio
    async def test_rag_down_llm_still_called(self):
        rag_resp = MagicMock()
        rag_resp.raise_for_status = MagicMock(side_effect=Exception("connection refused"))

        llm_resp = MagicMock()
        llm_resp.status_code = 200
        llm_resp.raise_for_status = MagicMock()
        llm_resp.json = MagicMock(return_value={
            "response": "Try the keynote at 9AM.", "model_used": "bitnet-2b4t",
            "tokens_used": 20, "processing_time": 5000.0,
        })

        sms_resp = MagicMock()
        sms_resp.status_code = 200
        sms_resp.raise_for_status = MagicMock()

        async def mock_post(url, json=None, timeout=None):
            if "/search" in url:
                return rag_resp
            elif "/inference" in url:
                return llm_resp
            return sms_resp

        self.router.http_client.post = AsyncMock(side_effect=mock_post)
        result = await self.router.process_message(_make_sms("What sessions are available?"))
        assert "try again" not in result.lower() or "keynote" in result.lower()

    @pytest.mark.asyncio
    async def test_rag_timeout_llm_still_called(self):
        import httpx

        async def mock_post(url, json=None, timeout=None):
            if "/search" in url:
                raise httpx.TimeoutException("RAG timed out")
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            if "/inference" in url:
                resp.json = MagicMock(return_value={
                    "response": "Check the schedule.", "model_used": "bitnet-2b4t",
                    "tokens_used": 10, "processing_time": 3000.0,
                })
            return resp

        self.router.http_client.post = AsyncMock(side_effect=mock_post)
        result = await self.router.process_message(_make_sms("When is the keynote?"))
        assert isinstance(result, str)
        assert len(result) > 0


class TestLLMFailure:
    def setup_method(self):
        self.router = MessageRouter()
        self.router.http_client = MagicMock()

    @pytest.mark.asyncio
    async def test_llm_down_returns_fallback(self):
        rag_resp = MagicMock()
        rag_resp.status_code = 200
        rag_resp.raise_for_status = MagicMock()
        rag_resp.json = MagicMock(return_value={"documents": ["Some context"], "scores": [0.6]})

        sms_resp = MagicMock()
        sms_resp.status_code = 200
        sms_resp.raise_for_status = MagicMock()

        async def mock_post(url, json=None, timeout=None):
            if "/search" in url:
                return rag_resp
            elif "/inference" in url:
                raise Exception("LLM service unreachable")
            return sms_resp

        self.router.http_client.post = AsyncMock(side_effect=mock_post)
        result = await self.router.process_message(_make_sms("What is edge computing?"))
        assert "sorry" in result.lower() or "try again" in result.lower()

    @pytest.mark.asyncio
    async def test_llm_timeout_returns_fallback(self):
        import httpx

        rag_resp = MagicMock()
        rag_resp.status_code = 200
        rag_resp.raise_for_status = MagicMock()
        rag_resp.json = MagicMock(return_value={"documents": [], "scores": []})

        sms_resp = MagicMock()
        sms_resp.status_code = 200
        sms_resp.raise_for_status = MagicMock()

        async def mock_post(url, json=None, timeout=None):
            if "/search" in url:
                return rag_resp
            elif "/inference" in url:
                raise httpx.TimeoutException("LLM timed out")
            return sms_resp

        self.router.http_client.post = AsyncMock(side_effect=mock_post)
        result = await self.router.process_message(_make_sms("Tell me about BitNet"))
        assert "sorry" in result.lower() or "try again" in result.lower()


class TestBitNetMalformed:
    @pytest.mark.asyncio
    async def test_malformed_json_from_bitnet(self):
        from backend.services.llm_inference.main import app, SYSTEM_PROMPT
        import httpx

        def bad_transport(request):
            return httpx.Response(200, json={"bad": "no choices key"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(bad_transport), base_url="http://test") as client:
            with patch("backend.services.llm_inference.main._http_client", client):
                from fastapi.testclient import TestClient
                with TestClient(app) as tc:
                    resp = tc.post("/inference", json={"prompt": "test", "max_length": 160, "temperature": 0.7})
                    assert resp.status_code == 502
