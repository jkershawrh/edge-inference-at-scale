"""Unit tests for SMSEventStream (Kafka helper).

All Kafka I/O is mocked — no running Kafka instance required.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.shared.streams import SMSEventStream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream(
    bootstrap_servers: str = "localhost:9092",
    topic: str = "sms.inbound",
    group_name: str = "processors",
) -> SMSEventStream:
    return SMSEventStream(
        bootstrap_servers=bootstrap_servers,
        topic=topic,
        group_name=group_name,
    )


def _mock_producer():
    p = AsyncMock()
    p.start = AsyncMock()
    p.stop = AsyncMock()
    metadata = MagicMock()
    metadata.partition = 0
    metadata.offset = 42
    p.send_and_wait = AsyncMock(return_value=metadata)
    return p


def _mock_consumer():
    c = AsyncMock()
    c.start = AsyncMock()
    c.stop = AsyncMock()
    c.getmany = AsyncMock(return_value={})
    c.commit = AsyncMock()
    c.assignment = MagicMock(return_value=[])
    c.partitions_for_topic = MagicMock(return_value={0})
    c.position = AsyncMock(return_value=0)
    c.end_offsets = AsyncMock(return_value={})
    return c


def _mock_record(partition=0, offset=0, value=None):
    rec = MagicMock()
    rec.partition = partition
    rec.offset = offset
    rec.value = value or {"sender": "+15551234567", "content": "Hi"}
    return rec


# ===================================================================
# Lifecycle — connect / close
# ===================================================================


class TestConnect:
    """SMSEventStream.connect() creates Kafka producer and consumer."""

    @pytest.mark.asyncio
    async def test_connect_starts_producer_and_consumer(self):
        stream = _make_stream()
        mock_p = _mock_producer()
        mock_c = _mock_consumer()

        with patch("backend.shared.streams.AIOKafkaProducer", return_value=mock_p), \
             patch("backend.shared.streams.AIOKafkaConsumer", return_value=mock_c):
            await stream.connect()

        mock_p.start.assert_called_once()
        mock_c.start.assert_called_once()


class TestClose:
    """SMSEventStream.close() stops producer and consumer."""

    @pytest.mark.asyncio
    async def test_close_stops_both(self):
        stream = _make_stream()
        mock_p = _mock_producer()
        mock_c = _mock_consumer()
        stream._producer = mock_p
        stream._consumer = mock_c

        await stream.close()

        mock_p.stop.assert_called_once()
        mock_c.stop.assert_called_once()
        assert stream._producer is None
        assert stream._consumer is None


# ===================================================================
# Producer — publish
# ===================================================================


class TestPublish:
    """SMSEventStream.publish() wraps Kafka send_and_wait."""

    @pytest.mark.asyncio
    async def test_publish_calls_send_and_wait(self):
        stream = _make_stream()
        stream._producer = _mock_producer()

        fields = {"sender": "+15551234567", "content": "Hello"}
        msg_id = await stream.publish(fields)

        stream._producer.send_and_wait.assert_called_once_with(
            "sms.inbound", value=fields,
        )
        assert msg_id == "0-42"

    @pytest.mark.asyncio
    async def test_publish_raises_when_disconnected(self):
        stream = _make_stream()
        with pytest.raises(RuntimeError, match="not connected"):
            await stream.publish({"sender": "+1"})


# ===================================================================
# Consumer — consume / ack
# ===================================================================


class TestConsume:
    """SMSEventStream.consume() wraps Kafka getmany."""

    @pytest.mark.asyncio
    async def test_consume_returns_messages(self):
        from aiokafka import TopicPartition
        stream = _make_stream()
        stream._consumer = _mock_consumer()

        tp = TopicPartition("sms.inbound", 0)
        records = [
            _mock_record(0, 0, {"sender": "+15551234567", "content": "Hi"}),
            _mock_record(0, 1, {"sender": "+15559999999", "content": "Bye"}),
        ]
        stream._consumer.getmany = AsyncMock(return_value={tp: records})

        messages = await stream.consume(consumer_name="worker-1", count=10)

        assert len(messages) == 2
        assert messages[0][0] == "0-0"
        assert messages[0][1]["content"] == "Hi"
        assert messages[1][0] == "0-1"
        assert messages[1][1]["content"] == "Bye"

    @pytest.mark.asyncio
    async def test_consume_empty_returns_empty_list(self):
        stream = _make_stream()
        stream._consumer = _mock_consumer()
        stream._consumer.getmany = AsyncMock(return_value={})

        messages = await stream.consume(consumer_name="worker-1")
        assert messages == []


class TestAck:
    """SMSEventStream.ack() commits the offset."""

    @pytest.mark.asyncio
    async def test_ack_commits_offset(self):
        from aiokafka import TopicPartition
        stream = _make_stream()
        stream._consumer = _mock_consumer()

        tp = TopicPartition("sms.inbound", 0)
        stream._pending["0-5"] = (tp, 5)

        await stream.ack("0-5")

        stream._consumer.commit.assert_called_once_with({tp: 6})
        assert "0-5" not in stream._pending

    @pytest.mark.asyncio
    async def test_ack_unknown_message_warns(self):
        stream = _make_stream()
        stream._consumer = _mock_consumer()

        await stream.ack("unknown-id")
        stream._consumer.commit.assert_not_called()


# ===================================================================
# Observability — health
# ===================================================================


class TestHealth:
    """SMSEventStream.health() returns topic metadata."""

    @pytest.mark.asyncio
    async def test_health_returns_topic_info(self):
        stream = _make_stream()
        stream._consumer = _mock_consumer()
        stream._consumer.partitions_for_topic = MagicMock(return_value={0, 1})

        info = await stream.health()

        assert info["status"] == "connected"
        assert info["topic"] == "sms.inbound"
        assert info["partitions"] == 2
        assert info["group"] == "processors"

    @pytest.mark.asyncio
    async def test_health_when_disconnected(self):
        stream = _make_stream()
        assert stream._consumer is None

        info = await stream.health()
        assert info == {"status": "disconnected"}
