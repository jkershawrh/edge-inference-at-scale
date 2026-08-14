"""Per-user chat history backed by an in-memory dict with TTL eviction.

Stores conversation turns (user + assistant pairs) per phone number so
the LLM can receive prior context.  Also provides lightweight treasure
hunt state helpers used by Feature 3.

Pure-stdlib implementation -- no external dependencies required.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger("chat-history")

_HUNT_TTL_SECONDS = 86400  # 24 hours


class ChatHistoryStore:
    """Manage per-user chat history and treasure hunt state in memory."""

    def __init__(
        self,
        max_turns: int = 10,
        ttl_seconds: int = 3600,
    ) -> None:
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds

        # chat history: phone -> list of {"role": ..., "content": ...}
        self._history: Dict[str, List[Dict[str, str]]] = {}
        self._history_ts: Dict[str, float] = {}

        # treasure hunt state: phone -> clue number
        self._hunt_state: Dict[str, int] = {}
        self._hunt_ts: Dict[str, float] = {}

        self._eviction_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Start the background eviction loop."""
        self._eviction_task = asyncio.create_task(self._evict_loop())
        logger.info("ChatHistoryStore connected (in-memory, ttl=%ds)", self.ttl_seconds)

    async def close(self) -> None:
        """Cancel the background eviction loop."""
        if self._eviction_task is not None:
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass
            self._eviction_task = None
            logger.info("ChatHistoryStore closed")

    # ------------------------------------------------------------------
    # Chat history
    # ------------------------------------------------------------------

    async def add_turn(
        self, phone_number: str, user_message: str, assistant_response: str
    ) -> None:
        """Append a user/assistant turn and trim to *max_turns*."""
        try:
            if phone_number not in self._history:
                self._history[phone_number] = []

            self._history[phone_number].append({"role": "user", "content": user_message})
            self._history[phone_number].append({"role": "assistant", "content": assistant_response})

            # Keep only the most recent max_turns * 2 entries (pairs)
            max_entries = self.max_turns * 2
            if len(self._history[phone_number]) > max_entries:
                self._history[phone_number] = self._history[phone_number][-max_entries:]

            self._history_ts[phone_number] = time.monotonic()
        except Exception as exc:
            logger.warning("Failed to store chat turn for %s: %s", phone_number, exc)

    async def get_history(self, phone_number: str) -> List[Dict[str, str]]:
        """Return the conversation history for *phone_number*.

        Returns an empty list when no history exists (graceful degradation).
        """
        try:
            history = self._history.get(phone_number, [])
            if history:
                self._history_ts[phone_number] = time.monotonic()
            return list(history)  # return a copy
        except Exception as exc:
            logger.warning("Failed to retrieve history for %s: %s", phone_number, exc)
            return []

    async def clear_history(self, phone_number: str) -> None:
        """Delete the conversation history for *phone_number*."""
        try:
            self._history.pop(phone_number, None)
            self._history_ts.pop(phone_number, None)
        except Exception as exc:
            logger.warning("Failed to clear history for %s: %s", phone_number, exc)

    # ------------------------------------------------------------------
    # Treasure hunt state (Feature 3)
    # ------------------------------------------------------------------

    async def get_hunt_state(self, phone_number: str) -> int:
        """Return the current clue number (0 if not started)."""
        try:
            return self._hunt_state.get(phone_number, 0)
        except Exception as exc:
            logger.warning("Failed to get hunt state for %s: %s", phone_number, exc)
            return 0

    async def set_hunt_state(self, phone_number: str, clue_number: int) -> None:
        """Set the current clue number with a 24-hour TTL."""
        try:
            self._hunt_state[phone_number] = clue_number
            self._hunt_ts[phone_number] = time.monotonic()
        except Exception as exc:
            logger.warning("Failed to set hunt state for %s: %s", phone_number, exc)

    async def clear_hunt_state(self, phone_number: str) -> None:
        """Clear the treasure hunt state for *phone_number*."""
        try:
            self._hunt_state.pop(phone_number, None)
            self._hunt_ts.pop(phone_number, None)
        except Exception as exc:
            logger.warning("Failed to clear hunt state for %s: %s", phone_number, exc)

    # ------------------------------------------------------------------
    # Background eviction
    # ------------------------------------------------------------------

    async def _evict_loop(self) -> None:
        """Periodically scan timestamps and evict expired entries."""
        while True:
            try:
                await asyncio.sleep(60)
                now = time.monotonic()

                # Evict expired chat histories
                expired_history = [
                    phone
                    for phone, ts in self._history_ts.items()
                    if now - ts > self.ttl_seconds
                ]
                for phone in expired_history:
                    self._history.pop(phone, None)
                    self._history_ts.pop(phone, None)

                # Evict expired hunt states (24h TTL)
                expired_hunt = [
                    phone
                    for phone, ts in self._hunt_ts.items()
                    if now - ts > _HUNT_TTL_SECONDS
                ]
                for phone in expired_hunt:
                    self._hunt_state.pop(phone, None)
                    self._hunt_ts.pop(phone, None)

                total_evicted = len(expired_history) + len(expired_hunt)
                if total_evicted:
                    logger.info(
                        "Evicted %d expired entries (%d history, %d hunt)",
                        total_evicted,
                        len(expired_history),
                        len(expired_hunt),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Eviction loop error: %s", exc)
