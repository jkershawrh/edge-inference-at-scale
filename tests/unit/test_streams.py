"""Unit tests for SMSEventStream (Redis Streams helper).

All Redis I/O is mocked — no running Redis instance required.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.shared.streams import SMSEventStream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream(
    redis_url: str = "redis://localhost:6379/0",
    stream_name: str = "sms:inbound",
    group_name: str = "processors",
) -> SMSEventStream:
    return SMSEventStream(
        redis_url=redis_url,
        stream_name=stream_name,
        group_name=group_name,
    )


def _mock_redis():
    """Return an AsyncMock that behaves like an ``aioredis.Redis`` instance."""
    r = AsyncMock()
    r.xgroup_create = AsyncMock()
    r.xadd = AsyncMock(return_value="1700000000000-0")
    r.xreadgroup = AsyncMock(return_value=None)
    r.xack = AsyncMock()
    r.xinfo_stream = AsyncMock(return_value={
        "length": 42,
        "last-generated-id": "1700000000000-0",
    })
    r.xinfo_groups = AsyncMock(return_value=[
        {
            "name": "processors",
            "consumers": 1,
            "pending": 0,
            "last-delivered-id": "1700000000000-0",
        }
    ])
    r.aclose = AsyncMock()
    return r


# ===================================================================
# Lifecycle — connect / close
# ===================================================================


class TestConnect:
    """SMSEventStream.connect() creates a consumer group via MKSTREAM."""

    @pytest.mark.asyncio
    async def test_connect_creates_consumer_group(self):
        stream = _make_stream()
        mock_r = _mock_redis()

        with patch("backend.shared.streams.aioredis.from_url", return_value=mock_r):
            await stream.connect()

        mock_r.xgroup_create.assert_called_once_with(
            name="sms:inbound",
            groupname="processors",
            id="0",
            mkstream=True,
        )

    @pytest.mark.asyncio
    async def test_connect_handles_existing_group(self):
        """ResponseError with 'BUSYGROUP' is caught silently."""
        import redis.asyncio as aioredis

        stream = _make_stream()
        mock_r = _mock_redis()
        mock_r.xgroup_create = AsyncMock(
            side_effect=aioredis.ResponseError("BUSYGROUP Consumer Group name already exists")
        )

        with patch("backend.shared.streams.aioredis.from_url", return_value=mock_r):
            # Should NOT raise
            await stream.connect()

        # The stream object should still be connected
        assert stream._redis is mock_r


class TestClose:
    """SMSEventStream.close() tears down the Redis connection."""

    @pytest.mark.asyncio
    async def test_close_calls_aclose(self):
        stream = _make_stream()
        mock_r = _mock_redis()
        stream._redis = mock_r

        await stream.close()

        mock_r.aclose.assert_called_once()
        assert stream._redis is None


# ===================================================================
# Producer — publish
# ===================================================================


class TestPublish:
    """SMSEventStream.publish() wraps XADD."""

    @pytest.mark.asyncio
    async def test_publish_calls_xadd(self):
        stream = _make_stream()
        mock_r = _mock_redis()
        stream._redis = mock_r

        fields = {"sender": "+15551234567", "content": "Hello"}
        await stream.publish(fields)

        mock_r.xadd.assert_called_once()
        call_kwargs = mock_r.xadd.call_args
        assert call_kwargs.kwargs["name"] == "sms:inbound"
        assert call_kwargs.kwargs["fields"] is fields

    @pytest.mark.asyncio
    async def test_publish_returns_message_id(self):
        stream = _make_stream()
        mock_r = _mock_redis()
        mock_r.xadd = AsyncMock(return_value="1700000000001-0")
        stream._redis = mock_r

        msg_id = await stream.publish({"sender": "+1", "content": "test"})
        assert msg_id == "1700000000001-0"


# ===================================================================
# Consumer — consume / ack
# ===================================================================


class TestConsume:
    """SMSEventStream.consume() wraps XREADGROUP."""

    @pytest.mark.asyncio
    async def test_consume_returns_messages(self):
        stream = _make_stream()
        mock_r = _mock_redis()

        # Redis XREADGROUP returns [[stream_name, [(id, fields), ...]]]
        mock_r.xreadgroup = AsyncMock(return_value=[
            ("sms:inbound", [
                ("1700000000000-0", {"sender": "+15551234567", "content": "Hi"}),
                ("1700000000001-0", {"sender": "+15559999999", "content": "Bye"}),
            ])
        ])
        stream._redis = mock_r

        messages = await stream.consume(consumer_name="worker-1", count=10)

        assert len(messages) == 2
        assert messages[0] == ("1700000000000-0", {"sender": "+15551234567", "content": "Hi"})
        assert messages[1] == ("1700000000001-0", {"sender": "+15559999999", "content": "Bye"})

    @pytest.mark.asyncio
    async def test_consume_empty_returns_empty_list(self):
        stream = _make_stream()
        mock_r = _mock_redis()
        mock_r.xreadgroup = AsyncMock(return_value=None)
        stream._redis = mock_r

        messages = await stream.consume(consumer_name="worker-1")
        assert messages == []


class TestAck:
    """SMSEventStream.ack() wraps XACK."""

    @pytest.mark.asyncio
    async def test_ack_calls_xack(self):
        stream = _make_stream()
        mock_r = _mock_redis()
        stream._redis = mock_r

        await stream.ack("1700000000000-0")

        mock_r.xack.assert_called_once_with("sms:inbound", "processors", "1700000000000-0")


# ===================================================================
# Observability — health
# ===================================================================


class TestHealth:
    """SMSEventStream.health() returns stream metadata."""

    @pytest.mark.asyncio
    async def test_health_returns_stream_info(self):
        stream = _make_stream()
        mock_r = _mock_redis()
        stream._redis = mock_r

        info = await stream.health()

        assert info["status"] == "connected"
        assert info["stream"] == "sms:inbound"
        assert info["length"] == 42
        assert info["last_generated_id"] == "1700000000000-0"
        assert info["groups"] == 1
        assert len(info["group_details"]) == 1
        assert info["group_details"][0]["name"] == "processors"

        mock_r.xinfo_stream.assert_called_once_with("sms:inbound")
        mock_r.xinfo_groups.assert_called_once_with("sms:inbound")

    @pytest.mark.asyncio
    async def test_health_when_disconnected(self):
        stream = _make_stream()
        # _redis is None by default (never connected)
        assert stream._redis is None

        info = await stream.health()

        assert info == {"status": "disconnected"}
