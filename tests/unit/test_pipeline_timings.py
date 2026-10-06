"""Tests for privacy-safe aggregate pipeline timing metrics."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.message_router.main import MessageRouter
from backend.shared.models import SMSMessage


def _message() -> SMSMessage:
    return SMSMessage(
        sender="+15551234567",
        receiver="+15559876543",
        content="hello",
    )


@pytest.mark.asyncio
async def test_pipeline_and_delivery_timings_are_recorded():
    router = MessageRouter()
    router.http_client = MagicMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    router.http_client.post = AsyncMock(return_value=response)

    result = await router.process_message(_message())
    timings = router.timing_snapshot()

    assert result.startswith("Welcome to Summit Connect")
    assert timings["pipeline"]["samples"] == 1
    assert timings["delivery"]["samples"] == 1
    assert timings["pipeline"]["average_ms"] >= 0


@pytest.mark.asyncio
async def test_rag_timing_records_failures_too():
    router = MessageRouter()
    router.http_client = MagicMock()
    router.http_client.post = AsyncMock(side_effect=RuntimeError("offline"))

    result = await router.route_to_rag("where is shelter")
    timings = router.timing_snapshot()

    assert result == (None, 0.0, None)
    assert timings["rag"]["samples"] == 1
