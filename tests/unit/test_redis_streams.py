"""Unit contracts for the lightweight Redis Streams field backend."""

import json
from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis

from backend.shared.streams import RedisSMSEventStream, create_sms_event_stream


def _client():
    client = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    client.xgroup_create = AsyncMock(return_value=True)
    client.aclose = AsyncMock()
    client.xadd = AsyncMock(return_value="1-0")
    client.xreadgroup = AsyncMock(return_value=[])
    client.xautoclaim = AsyncMock(return_value=["0-0", [], []])
    client.xack = AsyncMock(return_value=1)
    client.xlen = AsyncMock(return_value=0)
    return client


@pytest.mark.asyncio
async def test_consumer_connect_creates_group_and_stream():
    client = _client()
    stream = RedisSMSEventStream(
        "redis://redis:6379/0", enable_producer=False, enable_consumer=True
    )
    with patch("backend.shared.streams.aioredis.from_url", return_value=client):
        await stream.connect()

    client.ping.assert_awaited_once()
    client.xgroup_create.assert_awaited_once_with(
        name="sms.inbound", groupname="processors", id="0", mkstream=True
    )


@pytest.mark.asyncio
async def test_existing_consumer_group_is_accepted():
    client = _client()
    client.xgroup_create.side_effect = aioredis.ResponseError(
        "BUSYGROUP Consumer Group name already exists"
    )
    stream = RedisSMSEventStream(
        "redis://redis:6379/0", enable_producer=False, enable_consumer=True
    )
    with patch("backend.shared.streams.aioredis.from_url", return_value=client):
        await stream.connect()

    assert stream._redis is client


@pytest.mark.asyncio
async def test_failed_connect_cleans_up_client():
    client = _client()
    client.ping.side_effect = OSError("redis unavailable")
    stream = RedisSMSEventStream("redis://redis:6379/0")
    with patch("backend.shared.streams.aioredis.from_url", return_value=client):
        with pytest.raises(OSError, match="unavailable"):
            await stream.connect()

    client.aclose.assert_awaited_once()
    assert stream._redis is None


@pytest.mark.asyncio
async def test_publish_uses_one_json_envelope():
    client = _client()
    stream = RedisSMSEventStream(
        "redis://redis:6379/0", enable_producer=True, enable_consumer=False
    )
    stream._redis = client
    message = {"sender": "+1", "parsed": {"intent": "question"}}

    message_id = await stream.publish(message)

    assert message_id == "1-0"
    call = client.xadd.await_args.kwargs
    assert call["name"] == "sms.inbound"
    assert json.loads(call["fields"]["payload"]) == message


@pytest.mark.asyncio
async def test_consume_decodes_json_envelope_and_acknowledges():
    client = _client()
    client.xreadgroup.return_value = [
        ("sms.inbound", [("1-0", {"payload": '{"sender":"+1"}'})])
    ]
    stream = RedisSMSEventStream(
        "redis://redis:6379/0", enable_producer=False, enable_consumer=True
    )
    stream._redis = client

    messages = await stream.consume("edge-node-1")
    await stream.ack("1-0")

    assert messages == [("1-0", {"sender": "+1"})]
    client.xack.assert_awaited_once_with("sms.inbound", "processors", "1-0")


@pytest.mark.asyncio
async def test_consume_recovers_stale_pending_message_before_new_messages():
    client = _client()
    client.xautoclaim.return_value = [
        "0-0",
        [("2-0", {"payload": '{"sender":"+2"}'})],
        [],
    ]
    stream = RedisSMSEventStream(
        "redis://redis:6379/0", enable_producer=False, enable_consumer=True
    )
    stream._redis = client

    messages = await stream.consume("replacement-pod")

    assert messages == [("2-0", {"sender": "+2"})]
    client.xreadgroup.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_declares_backend_role_and_identity():
    stream = RedisSMSEventStream(
        "redis://redis:6379/0", enable_producer=True, enable_consumer=False
    )

    assert await stream.health() == {
        "status": "disconnected",
        "backend": "redis",
        "topic": "sms.inbound",
        "roles": ["producer"],
    }


def test_factory_selects_redis_and_rejects_unknown_backend():
    stream = create_sms_event_stream(
        backend="redis",
        redis_url="redis://redis:6379/0",
        kafka_bootstrap_servers="kafka:9092",
        stream_name="sms.inbound",
        group_name="processors",
        enable_producer=True,
        enable_consumer=False,
    )
    assert isinstance(stream, RedisSMSEventStream)

    with pytest.raises(ValueError, match="redis or kafka"):
        create_sms_event_stream(
            backend="unknown",
            redis_url="redis://redis:6379/0",
            kafka_bootstrap_servers="kafka:9092",
            stream_name="sms.inbound",
            group_name="processors",
            enable_producer=True,
            enable_consumer=False,
        )
