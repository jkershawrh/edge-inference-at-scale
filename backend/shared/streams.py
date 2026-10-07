"""Kafka helper for ordered, persistent SMS event delivery.

Provides at-least-once delivery semantics between the SMS Gateway
(producer) and the Message Router (consumer) using Kafka with
consumer groups.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
import redis.asyncio as aioredis

from backend.shared.config import settings

logger = logging.getLogger("sms-stream")


class RedisSMSEventStream:
    """Lightweight durable SMS stream backed by Redis Streams."""

    def __init__(
        self,
        redis_url: str,
        stream_name: str = "sms.inbound",
        group_name: str = "processors",
        enable_producer: bool = True,
        enable_consumer: bool = True,
    ) -> None:
        if not enable_producer and not enable_consumer:
            raise ValueError("stream must enable a producer or consumer")
        self.redis_url = redis_url
        self.stream_name = stream_name
        self.group_name = group_name
        self.enable_producer = enable_producer
        self.enable_consumer = enable_consumer
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Connect, verify the server, and create the consumer group."""
        client = aioredis.from_url(
            self.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=10,
            health_check_interval=15,
        )
        try:
            await client.ping()
            if self.enable_consumer:
                try:
                    await client.xgroup_create(
                        name=self.stream_name,
                        groupname=self.group_name,
                        id="0",
                        mkstream=True,
                    )
                except aioredis.ResponseError as exc:
                    if "BUSYGROUP" not in str(exc):
                        raise
        except BaseException:
            try:
                await client.aclose()
            except Exception:
                logger.debug("Failed Redis cleanup after startup error", exc_info=True)
            self._redis = None
            raise
        self._redis = client
        logger.info(
            "Redis stream connected, stream='%s', group='%s'",
            self.stream_name,
            self.group_name,
        )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
            logger.info("Redis stream connection closed")

    async def publish(self, message_data: dict) -> str:
        if self._redis is None:
            raise RuntimeError("SMSEventStream is not connected")
        message_id = await self._redis.xadd(
            name=self.stream_name,
            fields={"payload": json.dumps(message_data, separators=(",", ":"))},
            maxlen=settings.stream_max_len,
            approximate=True,
        )
        return str(message_id)

    async def consume(
        self,
        consumer_name: str,
        count: int = 1,
        block_ms: int = 5000,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        if self._redis is None:
            raise RuntimeError("SMSEventStream is not connected")
        claimed = await self._redis.xautoclaim(
            name=self.stream_name,
            groupname=self.group_name,
            consumername=consumer_name,
            min_idle_time=settings.stream_claim_idle_ms,
            start_id="0-0",
            count=count,
        )
        claimed_entries = claimed[1] if claimed and len(claimed) > 1 else []
        if claimed_entries:
            result = [(self.stream_name, claimed_entries)]
        else:
            result = await self._redis.xreadgroup(
                groupname=self.group_name,
                consumername=consumer_name,
                streams={self.stream_name: ">"},
                count=count,
                block=block_ms,
            )
        messages: List[Tuple[str, Dict[str, Any]]] = []
        for _stream, entries in result or []:
            for message_id, fields in entries:
                payload = fields.get("payload")
                if payload is None:
                    logger.warning("Ignoring stream message %s without payload", message_id)
                    continue
                messages.append((str(message_id), json.loads(payload)))
        return messages

    async def ack(self, message_id: str) -> None:
        if self._redis is None:
            raise RuntimeError("SMSEventStream is not connected")
        await self._redis.xack(self.stream_name, self.group_name, message_id)

    async def pending(self) -> dict:
        if self._redis is None:
            raise RuntimeError("SMSEventStream is not connected")
        info = await self._redis.xpending(self.stream_name, self.group_name)
        if isinstance(info, dict):
            return {
                "total_lag": info.get("pending", 0),
                "min_id": info.get("min"),
                "max_id": info.get("max"),
                "consumers": info.get("consumers", []),
            }
        return {
            "total_lag": info[0],
            "min_id": info[1],
            "max_id": info[2],
            "consumers": info[3],
        }

    async def health(self) -> dict:
        info: Dict[str, Any] = {
            "status": "disconnected" if self._redis is None else "connected",
            "backend": "redis",
            "topic": self.stream_name,
            "roles": [
                role
                for role, enabled in (
                    ("producer", self.enable_producer),
                    ("consumer", self.enable_consumer),
                )
                if enabled
            ],
        }
        if self.enable_consumer:
            info["group"] = self.group_name
        if self._redis is None:
            return info
        try:
            await self._redis.ping()
            info["length"] = await self._redis.xlen(self.stream_name)
            return info
        except Exception as exc:
            logger.warning("Redis stream health check failed: %s", exc)
            info.update({"status": "error", "error": str(exc)})
            return info


class SMSEventStream:
    """Publish / consume SMS events via Kafka with consumer groups."""

    def __init__(
        self,
        bootstrap_servers: str,
        topic: str = "sms.inbound",
        group_name: str = "processors",
        enable_producer: bool = True,
        enable_consumer: bool = True,
    ) -> None:
        if not enable_producer and not enable_consumer:
            raise ValueError("stream must enable a producer or consumer")
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.stream_name = topic
        self.group_name = group_name
        self.enable_producer = enable_producer
        self.enable_consumer = enable_consumer
        self._producer: Optional[AIOKafkaProducer] = None
        self._consumer: Optional[AIOKafkaConsumer] = None
        # Map message_id -> TopicPartition + offset for manual commit
        self._pending: Dict[str, Tuple[TopicPartition, int]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create and start only the Kafka roles requested by this service."""
        producer: Optional[AIOKafkaProducer] = None
        if self.enable_producer:
            producer = AIOKafkaProducer(
                bootstrap_servers=self.bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            )
            try:
                await producer.start()
            except BaseException:
                # A half-started producer must never be visible to publish(); it
                # would make the SMS worker hang instead of using HTTP fallback.
                try:
                    await producer.stop()
                except Exception:
                    logger.debug("Failed producer cleanup after startup error", exc_info=True)
                self._producer = None
                self._consumer = None
                raise
            self._producer = producer
            logger.info(
                "Kafka producer started, bootstrap_servers=%s",
                self.bootstrap_servers,
            )

        if self.enable_consumer:
            consumer = AIOKafkaConsumer(
                self.topic,
                bootstrap_servers=self.bootstrap_servers,
                group_id=self.group_name,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            )
            try:
                await consumer.start()
            except BaseException:
                try:
                    await consumer.stop()
                except Exception:
                    logger.debug("Failed consumer cleanup after startup error", exc_info=True)
                if producer is not None:
                    try:
                        await producer.stop()
                    except Exception:
                        logger.debug("Failed producer cleanup after consumer error", exc_info=True)
                self._producer = None
                self._consumer = None
                raise
            self._consumer = consumer
            logger.info(
                "Kafka consumer started, topic='%s', group='%s'",
                self.topic,
                self.group_name,
            )
        logger.info(
            "SMSEventStream connected to %s", self.bootstrap_servers,
        )

    async def close(self) -> None:
        """Stop the Kafka producer and consumer."""
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
            logger.info("Kafka producer stopped")
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
            logger.info("Kafka consumer stopped")
        self._pending.clear()
        logger.info("SMSEventStream connection closed")

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------

    async def publish(self, message_data: dict) -> str:
        """Send a message to the Kafka topic and return a message ID.

        Expected fields: sender, receiver, content, timestamp, priority.
        Returns a string of the form ``"partition-offset"``.
        """
        if self._producer is None:
            raise RuntimeError("SMSEventStream is not connected")

        metadata = await self._producer.send_and_wait(
            self.topic, value=message_data,
        )
        msg_id = f"{metadata.partition}-{metadata.offset}"
        logger.info("Published message %s to topic '%s'", msg_id, self.topic)
        return msg_id

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------

    async def consume(
        self,
        consumer_name: str,
        count: int = 1,
        block_ms: int = 5000,
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Fetch new messages for *consumer_name*.

        Returns a list of ``(message_id, fields)`` tuples.
        Blocks for up to *block_ms* milliseconds when no messages are
        available.
        """
        if self._consumer is None:
            raise RuntimeError("SMSEventStream is not connected")

        result = await self._consumer.getmany(
            timeout_ms=block_ms,
            max_records=count,
        )

        messages: List[Tuple[str, Dict[str, Any]]] = []
        for tp, records in result.items():
            for record in records:
                msg_id = f"{record.partition}-{record.offset}"
                self._pending[msg_id] = (tp, record.offset)
                messages.append((msg_id, record.value))
        return messages

    async def ack(self, message_id: str) -> None:
        """Commit the offset for a successfully processed message."""
        if self._consumer is None:
            raise RuntimeError("SMSEventStream is not connected")

        if message_id not in self._pending:
            logger.warning(
                "Cannot ACK unknown message %s (not in pending)", message_id,
            )
            return

        tp, offset = self._pending.pop(message_id)
        # Commit the *next* offset (offset + 1) so the consumer resumes
        # after this message on restart.
        await self._consumer.commit({tp: offset + 1})
        logger.debug("ACKed message %s", message_id)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def pending(self) -> dict:
        """Return consumer lag info for the subscribed topic."""
        if self._consumer is None:
            raise RuntimeError("SMSEventStream is not connected")

        partitions = self._consumer.assignment()
        lag_details = []
        total_lag = 0
        for tp in partitions:
            position = await self._consumer.position(tp)
            end_offsets = await self._consumer.end_offsets([tp])
            end = end_offsets[tp]
            lag = max(0, end - position)
            total_lag += lag
            lag_details.append(
                {
                    "partition": tp.partition,
                    "position": position,
                    "end_offset": end,
                    "lag": lag,
                }
            )
        return {
            "total_lag": total_lag,
            "pending_local": len(self._pending),
            "partitions": lag_details,
        }

    async def health(self) -> dict:
        """Return topic metadata for the configured topic."""
        producer_connected = not self.enable_producer or self._producer is not None
        consumer_connected = not self.enable_consumer or self._consumer is not None
        info = {
            "status": "connected" if producer_connected and consumer_connected else "disconnected",
            "backend": "kafka",
            "topic": self.topic,
            "roles": [
                role
                for role, enabled in (
                    ("producer", self.enable_producer),
                    ("consumer", self.enable_consumer),
                )
                if enabled
            ],
        }
        if self.enable_consumer:
            info["group"] = self.group_name
        if info["status"] != "connected" or self._consumer is None:
            return info
        try:
            partitions = self._consumer.partitions_for_topic(self.topic)
            info.update(
                {
                    "partitions": len(partitions) if partitions else 0,
                    "assignment": [
                        {"topic": tp.topic, "partition": tp.partition}
                        for tp in self._consumer.assignment()
                    ],
                }
            )
            return info
        except Exception as exc:
            logger.warning("Topic health check failed: %s", exc)
            info.update({"status": "error", "error": str(exc)})
            return info


def create_sms_event_stream(
    *,
    backend: str,
    redis_url: str,
    kafka_bootstrap_servers: str,
    stream_name: str,
    group_name: str,
    enable_producer: bool,
    enable_consumer: bool,
):
    """Build the configured stream backend behind one service interface."""
    normalized = backend.strip().lower()
    if normalized == "redis":
        return RedisSMSEventStream(
            redis_url=redis_url,
            stream_name=stream_name,
            group_name=group_name,
            enable_producer=enable_producer,
            enable_consumer=enable_consumer,
        )
    if normalized == "kafka":
        return SMSEventStream(
            bootstrap_servers=kafka_bootstrap_servers,
            topic=stream_name,
            group_name=group_name,
            enable_producer=enable_producer,
            enable_consumer=enable_consumer,
        )
    raise ValueError("STREAM_BACKEND must be redis or kafka")
