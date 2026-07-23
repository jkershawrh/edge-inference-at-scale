"""Per-user chat history backed by Redis lists.

Stores conversation turns (user + assistant pairs) per phone number so
the LLM can receive prior context.  Also provides lightweight treasure
hunt state helpers used by Feature 3.

Follows the same ``redis.asyncio`` pattern established in ``streams.py``.
"""

import json
import logging
from typing import Dict, List, Optional

import redis.asyncio as aioredis

logger = logging.getLogger("chat-history")


class ChatHistoryStore:
    """Manage per-user chat history and treasure hunt state in Redis."""

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "chat:",
        max_turns: int = 10,
        ttl_seconds: int = 3600,
    ) -> None:
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._redis: Optional[aioredis.Redis] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Redis."""
        self._redis = aioredis.from_url(
            self.redis_url,
            decode_responses=True,
        )
        logger.info("ChatHistoryStore connected to %s", self.redis_url)

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
            logger.info("ChatHistoryStore connection closed")

    # ------------------------------------------------------------------
    # Chat history
    # ------------------------------------------------------------------

    async def add_turn(
        self, phone_number: str, user_message: str, assistant_response: str
    ) -> None:
        """Append a user/assistant turn and trim to *max_turns*."""
        if self._redis is None:
            logger.warning("ChatHistoryStore not connected — skipping add_turn")
            return
        key = f"{self.key_prefix}{phone_number}"
        try:
            user_entry = json.dumps({"role": "user", "content": user_message})
            assistant_entry = json.dumps({"role": "assistant", "content": assistant_response})
            await self._redis.rpush(key, user_entry, assistant_entry)
            # Keep only the most recent max_turns * 2 entries (pairs)
            await self._redis.ltrim(key, -(self.max_turns * 2), -1)
            await self._redis.expire(key, self.ttl_seconds)
        except Exception as exc:
            logger.warning("Failed to store chat turn for %s: %s", phone_number, exc)

    async def get_history(self, phone_number: str) -> List[Dict[str, str]]:
        """Return the conversation history for *phone_number*.

        Returns an empty list when Redis is unavailable (graceful degradation).
        """
        if self._redis is None:
            logger.warning("ChatHistoryStore not connected — returning empty history")
            return []
        key = f"{self.key_prefix}{phone_number}"
        try:
            raw_entries = await self._redis.lrange(key, 0, -1)
            history: List[Dict[str, str]] = []
            for entry in raw_entries:
                try:
                    parsed = json.loads(entry)
                    history.append({"role": parsed["role"], "content": parsed["content"]})
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning("Skipping malformed history entry: %s", exc)
            return history
        except Exception as exc:
            logger.warning("Failed to retrieve history for %s: %s", phone_number, exc)
            return []

    async def clear_history(self, phone_number: str) -> None:
        """Delete the conversation history for *phone_number*."""
        if self._redis is None:
            return
        key = f"{self.key_prefix}{phone_number}"
        try:
            await self._redis.delete(key)
        except Exception as exc:
            logger.warning("Failed to clear history for %s: %s", phone_number, exc)

    # ------------------------------------------------------------------
    # Treasure hunt state (Feature 3)
    # ------------------------------------------------------------------

    async def get_hunt_state(self, phone_number: str) -> int:
        """Return the current clue number (0 if not started)."""
        if self._redis is None:
            return 0
        try:
            value = await self._redis.get(f"hunt:{phone_number}")
            return int(value) if value is not None else 0
        except Exception as exc:
            logger.warning("Failed to get hunt state for %s: %s", phone_number, exc)
            return 0

    async def set_hunt_state(self, phone_number: str, clue_number: int) -> None:
        """Set the current clue number with a 24-hour TTL."""
        if self._redis is None:
            return
        try:
            await self._redis.set(f"hunt:{phone_number}", clue_number, ex=86400)
        except Exception as exc:
            logger.warning("Failed to set hunt state for %s: %s", phone_number, exc)

    async def clear_hunt_state(self, phone_number: str) -> None:
        """Clear the treasure hunt state for *phone_number*."""
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"hunt:{phone_number}")
        except Exception as exc:
            logger.warning("Failed to clear hunt state for %s: %s", phone_number, exc)
