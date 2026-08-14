"""Kafka helper for ordered, persistent SMS event delivery.

Provides at-least-once delivery semantics between the SMS Gateway
(producer) and the Message Router (consumer) using Kafka with
consumer groups.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition

from backend.shared.config import settings

logger = logging.getLogger("sms-stream")


class SMSEventStream:
    """Publish / consume SMS events via Kafka with consumer groups."""

    def __init__(
        self,
        bootstrap_servers: str,
        topic: str = "sms.inbound",
        group_name: str = "processors",
    ) -> None:
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.group_name = group_name
        self._producer: Optional[AIOKafkaProducer] = None
        self._consumer: Optional[AIOKafkaConsumer] = None
        # Map message_id -> TopicPartition + offset for manual commit
        self._pending: Dict[str, Tuple[TopicPartition, int]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create and start the Kafka producer and consumer."""
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        await self._producer.start()
        logger.info(
            "Kafka producer started, bootstrap_servers=%s",
            self.bootstrap_servers,
        )

        self._consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_name,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        )
        await self._consumer.start()
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
        if self._consumer is None:
            return {"status": "disconnected"}
        try:
            partitions = self._consumer.partitions_for_topic(self.topic)
            return {
                "status": "connected",
                "topic": self.topic,
                "partitions": len(partitions) if partitions else 0,
                "group": self.group_name,
                "assignment": [
                    {"topic": tp.topic, "partition": tp.partition}
                    for tp in self._consumer.assignment()
                ],
            }
        except Exception as exc:
            logger.warning("Topic health check failed: %s", exc)
            return {"status": "error", "error": str(exc)}
