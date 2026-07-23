"""Concurrency tests — semaphore, queue backpressure, parallel classification."""

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.shared.models import SMSMessage, MessagePriority, MessageType
from backend.services.message_router.main import MessageRouter


def _make_sms(content):
    return SMSMessage(
        id=str(uuid.uuid4()), sender="+15551234567", receiver="+15559876543",
        content=content, timestamp=datetime.now(timezone.utc), priority=MessagePriority.NORMAL,
    )


class TestLLMSemaphore:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_llm_calls(self):
        router = MessageRouter()
        router.http_client = MagicMock()
        router.llm_semaphore = asyncio.Semaphore(1)

        call_times = []

        async def slow_llm(url, json=None, timeout=None):
            if "/inference" in url:
                call_times.append(("start", asyncio.get_event_loop().time()))
                await asyncio.sleep(0.05)
                call_times.append(("end", asyncio.get_event_loop().time()))
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(return_value={
                    "response": "Answer", "model_used": "bitnet", "tokens_used": 5, "processing_time": 50.0,
                })
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"documents": [], "scores": []})
            return resp

        router.http_client.post = AsyncMock(side_effect=slow_llm)

        results = await asyncio.gather(
            router.route_to_llm("question 1"),
            router.route_to_llm("question 2"),
        )
        assert all(r is not None for r in results)
        # With semaphore=1, second call starts after first ends
        if len(call_times) >= 4:
            first_end = call_times[1][1]
            second_start = call_times[2][1]
            assert second_start >= first_end - 0.01


class TestQueueBackpressure:
    @pytest.mark.asyncio
    async def test_queue_full_raises(self):
        q = asyncio.Queue(maxsize=2)
        await q.put("msg1")
        await q.put("msg2")
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait("msg3")


class TestParallelClassification:
    @pytest.mark.asyncio
    async def test_concurrent_classify(self):
        router = MessageRouter()
        messages = [
            _make_sms("What time is the keynote?"),
            _make_sms("Where is room A1?"),
            _make_sms("hello"),
            _make_sms("/status"),
            _make_sms("emergency fire help"),
            _make_sms("Tell me about BitNet"),
            _make_sms("Who is speaking?"),
            _make_sms("Best food nearby?"),
        ]

        async def classify(msg):
            return router.classify_message(msg)

        results = await asyncio.gather(*[classify(m) for m in messages])
        assert len(results) == 8
        assert all(r.message_type in MessageType for r in results)
        types = [r.message_type for r in results]
        assert MessageType.QUERY in types
        assert MessageType.EMERGENCY in types
