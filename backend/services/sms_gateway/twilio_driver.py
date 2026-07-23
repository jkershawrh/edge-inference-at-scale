"""Twilio SMS driver -- real SMS integration via the Twilio REST API.

Implements the same interface as SimDriver so the gateway can swap
between simulation and live SMS transparently.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sms-gateway.twilio")

try:
    from twilio.rest import Client as TwilioClient
    from twilio.base.exceptions import TwilioRestException

    _HAS_TWILIO = True
except ImportError:
    _HAS_TWILIO = False
    logger.warning(
        "twilio package not installed — run `pip install 'twilio>=9.0.0'` "
        "to enable Twilio SMS integration"
    )


class TwilioDriver:
    """Send and receive SMS via the Twilio REST API.

    The Twilio Python SDK is synchronous, so all blocking calls are
    dispatched to the default executor via ``run_in_executor``.
    """

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        phone_number: str,
    ) -> None:
        if not _HAS_TWILIO:
            raise ImportError(
                "twilio package is required but not installed. "
                "Install it with: pip install 'twilio>=9.0.0'"
            )
        self._account_sid = account_sid
        self._auth_token = auth_token
        self.phone_number = phone_number
        self._client: Optional["TwilioClient"] = None
        self._inbox: List[Dict[str, Any]] = []
        self._outbox: List[Dict[str, Any]] = []

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> bool:
        """Create the Twilio client and verify credentials.

        Returns ``True`` if the account is reachable, ``False`` otherwise.
        """
        loop = asyncio.get_event_loop()
        try:
            self._client = await loop.run_in_executor(
                None, lambda: TwilioClient(self._account_sid, self._auth_token)
            )
            # Verify credentials by fetching the account resource
            account = await loop.run_in_executor(
                None, lambda: self._client.api.accounts(self._account_sid).fetch()  # type: ignore[union-attr]
            )
            masked = self.phone_number[:3] + "xxx" + self.phone_number[-2:]
            logger.info(
                "[TWILIO] initialized with number %s (account: %s)",
                masked,
                account.friendly_name,
            )
            return True
        except Exception as exc:
            logger.error("[TWILIO] failed to initialize: %s", exc)
            return False

    # -- outbound ------------------------------------------------------------

    async def send_sms(self, phone_number: str, content: str) -> bool:
        """Send an SMS via Twilio.

        The SDK call is synchronous so it runs in the default thread-pool
        executor to avoid blocking the event loop.
        """
        if self._client is None:
            logger.error("[TWILIO] client not initialized — call initialize() first")
            return False

        loop = asyncio.get_event_loop()
        try:
            client = self._client
            from_number = self.phone_number

            def _send() -> Any:
                return client.messages.create(
                    to=phone_number,
                    from_=from_number,
                    body=content,
                )

            twilio_msg = await loop.run_in_executor(None, _send)

            msg: Dict[str, Any] = {
                "phone_number": phone_number,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "direction": "outbound",
                "sid": twilio_msg.sid,
            }
            self._outbox.append(msg)
            logger.info(
                "[TWILIO] Sent SMS to %s (sid=%s): %s",
                phone_number,
                twilio_msg.sid,
                content[:40],
            )
            return True
        except Exception as exc:
            logger.error("[TWILIO] Failed to send SMS to %s: %s", phone_number, exc)
            return False

    # -- inbound -------------------------------------------------------------

    async def receive_sms(self, phone_number: str, content: str) -> Dict[str, Any]:
        """Record an inbound SMS in the inbox and return it.

        Inbound messages arrive via the ``/twilio/webhook`` endpoint which
        calls this method — Twilio pushes to us; we do not poll.
        """
        msg: Dict[str, Any] = {
            "phone_number": phone_number,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": "inbound",
        }
        self._inbox.append(msg)
        logger.info("[TWILIO] Received SMS from %s: %s", phone_number, content[:40])
        return msg

    # -- history -------------------------------------------------------------

    async def get_outbox(self) -> List[Dict[str, Any]]:
        """Return a copy of all sent messages."""
        return list(self._outbox)

    async def get_inbox(self) -> List[Dict[str, Any]]:
        """Return a copy of all received messages."""
        return list(self._inbox)

    # -- teardown ------------------------------------------------------------

    async def disconnect(self) -> None:
        """No-op — the Twilio client uses plain HTTP, no persistent connection."""
        logger.info("[TWILIO] driver disconnected")
