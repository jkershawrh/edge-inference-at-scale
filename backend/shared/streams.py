"""Redis Streams helper for ordered, persistent SMS event delivery.

Provides at-least-once delivery semantics between the SMS Gateway
(producer) and the Message Router (consumer) using Redis Streams
with consumer groups.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import redis.asyncio as aioredis

from backend.shared.config import settings

logger = logging.getLogger("sms-stream")


class SMSEventStream:
    """Publish / consume SMS events via a Redis Stream with consumer groups."""

    def __init__(
        self,
        redis_url: str,
        stream_name: str = "sms:inbound",
        group_name: str = "processors",
    ) -> None:
        self.redis_url = redis_url
        self.stream_name = stream_name
        self.group_name = group_name
        self._redis: Optional[aioredis.Redis] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Redis and ensure the stream + consumer group exist."""
        self._redis = aioredis.from_url(
            self.redis_url,
            decode_responses=True,
        )
        # Create the consumer group (and the stream via MKSTREAM) if needed.
        try:
            await self._redis.xgroup_create(
                name=self.stream_name,
                groupname=self.group_name,
                id="0",
                mkstream=True,
            )
            logger.info(
                "Created consumer group '%s' on stream '%s'",
                self.group_name,
                self.stream_name,
            )
        except aioredis.ResponseError as exc:
            # "BUSYGROUP Consumer Group name already exists" is expected.
            if "BUSYGROUP" in str(exc):
                logger.debug(
                    "Consumer group '%s' already exists on stream '%s'",
                    self.group_name,
                    self.stream_name,
                )
            else:
                raise
        logger.info("SMSEventStream connected to %s", self.redis_url)

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
            logger.info("SMSEventStream connection closed")

    # ------------------------------------------------------------------
    # Producer
    # ------------------------------------------------------------------

    async def publish(self, message_data: dict) -> str:
        """XADD a message to the stream and return the generated ID.

        Expected fields: sender, receiver, content, timestamp, priority.
        The stream is capped at ``settings.stream_max_len`` entries.
        """
        if self._redis is None:
            raise RuntimeError("SMSEventStream is not connected")

        msg_id: str = await self._redis.xadd(
            name=self.stream_name,
            fields=message_data,
            maxlen=settings.stream_max_len,
            approximate=True,
        )
        logger.info("Published message %s to stream '%s'", msg_id, self.stream_name)
        return msg_id

    # ------------------------------------------------------------------
    # Consumer
    # ------------------------------------------------------------------

    async def consume(
        self,
        consumer_name: str,
        count: int = 1,
        block_ms: int = 5000,
    ) -> List[Tuple[str, Dict[str, str]]]:
        """XREADGROUP: fetch new messages for *consumer_name*.

        Returns a list of ``(message_id, fields)`` tuples.
        Blocks for up to *block_ms* milliseconds when no messages are
        available.
        """
        if self._redis is None:
            raise RuntimeError("SMSEventStream is not connected")

        result = await self._redis.xreadgroup(
            groupname=self.group_name,
            consumername=consumer_name,
            streams={self.stream_name: ">"},
            count=count,
            block=block_ms,
        )

        messages: List[Tuple[str, Dict[str, str]]] = []
        if result:
            # result is [[stream_name, [(id, fields), ...]]]
            for _stream, entries in result:
                for msg_id, fields in entries:
                    messages.append((msg_id, fields))
        return messages

    async def ack(self, message_id: str) -> None:
        """Acknowledge successful processing of a message."""
        if self._redis is None:
            raise RuntimeError("SMSEventStream is not connected")
        await self._redis.xack(self.stream_name, self.group_name, message_id)
        logger.debug("ACKed message %s", message_id)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def pending(self) -> dict:
        """Return XPENDING summary for the consumer group."""
        if self._redis is None:
            raise RuntimeError("SMSEventStream is not connected")
        info = await self._redis.xpending(self.stream_name, self.group_name)
        return {
            "pending_count": info.get("pending", 0) if isinstance(info, dict) else info[0],
            "min_id": info.get("min", None) if isinstance(info, dict) else info[1],
            "max_id": info.get("max", None) if isinstance(info, dict) else info[2],
            "consumers": info.get("consumers", []) if isinstance(info, dict) else info[3],
        }

    async def health(self) -> dict:
        """Return high-level stream info (length, groups, last generated ID)."""
        if self._redis is None:
            return {"status": "disconnected"}
        try:
            info = await self._redis.xinfo_stream(self.stream_name)
            groups = await self._redis.xinfo_groups(self.stream_name)
            return {
                "status": "connected",
                "stream": self.stream_name,
                "length": info.get("length", 0),
                "last_generated_id": info.get("last-generated-id", None),
                "groups": len(groups),
                "group_details": [
                    {
                        "name": g.get("name"),
                        "consumers": g.get("consumers"),
                        "pending": g.get("pending"),
                        "last_delivered_id": g.get("last-delivered-id"),
                    }
                    for g in groups
                ],
            }
        except Exception as exc:
            logger.warning("Stream health check failed: %s", exc)
            return {"status": "error", "error": str(exc)}
