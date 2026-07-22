"""Twilio SMS driver stub -- Phase 4 integration placeholder.

All methods raise ``NotImplementedError`` until real Twilio credentials
and SDK wiring are added.
"""


class TwilioDriver:
    """Same interface as SimDriver; every call raises NotImplementedError."""

    _ERR = "Twilio not configured — set SMS_MODE=twilio with valid credentials"

    async def initialize(self) -> bool:
        raise NotImplementedError(self._ERR)

    async def send_sms(self, phone_number: str, content: str) -> bool:
        raise NotImplementedError(self._ERR)

    async def receive_sms(self, phone_number: str, content: str):
        raise NotImplementedError(self._ERR)

    async def get_outbox(self) -> list:
        raise NotImplementedError(self._ERR)

    async def get_inbox(self) -> list:
        raise NotImplementedError(self._ERR)

    async def disconnect(self) -> None:
        raise NotImplementedError(self._ERR)
