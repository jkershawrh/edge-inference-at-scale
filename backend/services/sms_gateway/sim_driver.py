"""In-memory SMS simulation driver for the web UI demo.

Provides a fake SMS send/receive layer so the full pipeline can be
exercised without any hardware, Twilio account, or external service.
"""

import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

logger = logging.getLogger("sms-gateway.sim-driver")


class SimDriver:
    """Stores sent and received messages in plain Python lists."""

    def __init__(self) -> None:
        self._inbox: List[Dict[str, Any]] = []
        self._outbox: List[Dict[str, Any]] = []

    async def initialize(self) -> bool:
        """Always succeeds -- nothing to connect to."""
        logger.info("[SIM] Simulation driver initialised")
        return True

    async def send_sms(self, phone_number: str, content: str) -> bool:
        """Append a message to the in-memory outbox."""
        msg: Dict[str, Any] = {
            "phone_number": phone_number,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": "outbound",
        }
        self._outbox.append(msg)
        logger.info("[SIM] Sent SMS to %s: %s", phone_number, content[:40])
        return True

    async def receive_sms(self, phone_number: str, content: str) -> Dict[str, Any]:
        """Record an incoming message in the inbox and return it."""
        msg: Dict[str, Any] = {
            "phone_number": phone_number,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": "inbound",
        }
        self._inbox.append(msg)
        logger.info("[SIM] Received SMS from %s: %s", phone_number, content[:40])
        return msg

    async def get_outbox(self) -> List[Dict[str, Any]]:
        """Return all sent messages."""
        return list(self._outbox)

    async def get_inbox(self) -> List[Dict[str, Any]]:
        """Return all received messages."""
        return list(self._inbox)

    async def disconnect(self) -> None:
        """No-op -- nothing to tear down."""
        logger.info("[SIM] Simulation driver disconnected")
