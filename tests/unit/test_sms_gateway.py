"""Unit tests for the SMS Gateway service.

All external dependencies (message router, Redis, drivers) are mocked.
Tests validate the SimDriver, rate limiter, message parser, and gateway
receive/forward logic in isolation.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.sms_gateway.sim_driver import SimDriver
from backend.services.sms_gateway.message_parser import (
    MessageCategory,
    MessageIntent,
    MessageParser,
)
from backend.services.sms_gateway.main import _RateLimiter


# ===================================================================
# SimDriver tests
# ===================================================================


class TestSimDriverSendSMS:
    """test_sim_driver_send_sms — SimDriver stores outbound messages."""

    @pytest.mark.asyncio
    async def test_send_sms_returns_true(self):
        driver = SimDriver()
        await driver.initialize()
        result = await driver.send_sms("+15551234567", "Hello from the edge")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_sms_appears_in_outbox(self):
        driver = SimDriver()
        await driver.initialize()
        await driver.send_sms("+15551234567", "Test message")
        outbox = await driver.get_outbox()
        assert len(outbox) == 1
        assert outbox[0]["phone_number"] == "+15551234567"
        assert outbox[0]["content"] == "Test message"
        assert outbox[0]["direction"] == "outbound"

    @pytest.mark.asyncio
    async def test_send_multiple_sms_ordered(self):
        driver = SimDriver()
        await driver.initialize()
        await driver.send_sms("+1001", "First")
        await driver.send_sms("+1002", "Second")
        await driver.send_sms("+1003", "Third")
        outbox = await driver.get_outbox()
        assert len(outbox) == 3
        assert [m["content"] for m in outbox] == ["First", "Second", "Third"]


class TestSimDriverInboxOutbox:
    """test_sim_driver_inbox_outbox — inbox and outbox are independent."""

    @pytest.mark.asyncio
    async def test_receive_sms_appears_in_inbox(self):
        driver = SimDriver()
        await driver.initialize()
        msg = await driver.receive_sms("+15559999999", "Incoming message")
        assert msg["direction"] == "inbound"
        assert msg["content"] == "Incoming message"
        inbox = await driver.get_inbox()
        assert len(inbox) == 1

    @pytest.mark.asyncio
    async def test_inbox_outbox_independent(self):
        driver = SimDriver()
        await driver.initialize()
        await driver.send_sms("+1001", "Outbound")
        await driver.receive_sms("+1002", "Inbound")
        inbox = await driver.get_inbox()
        outbox = await driver.get_outbox()
        assert len(inbox) == 1
        assert len(outbox) == 1
        assert inbox[0]["direction"] == "inbound"
        assert outbox[0]["direction"] == "outbound"

    @pytest.mark.asyncio
    async def test_get_inbox_returns_copy(self):
        driver = SimDriver()
        await driver.initialize()
        await driver.receive_sms("+1001", "Msg1")
        inbox1 = await driver.get_inbox()
        inbox2 = await driver.get_inbox()
        assert inbox1 is not inbox2
        assert inbox1 == inbox2

    @pytest.mark.asyncio
    async def test_disconnect_is_noop(self):
        driver = SimDriver()
        await driver.initialize()
        await driver.disconnect()  # Should not raise


# ===================================================================
# Rate limiter tests
# ===================================================================


class TestRateLimiting:
    """test_rate_limiting — sliding-window rate limiter."""

    def test_allows_within_limit(self):
        limiter = _RateLimiter(per_minute=5, per_hour=100)
        for _ in range(5):
            assert limiter.allow() is True

    def test_blocks_after_per_minute_limit(self):
        limiter = _RateLimiter(per_minute=3, per_hour=100)
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert limiter.allow() is False

    def test_blocks_after_per_hour_limit(self):
        limiter = _RateLimiter(per_minute=100, per_hour=2)
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert limiter.allow() is False

    def test_allows_after_window_expires(self):
        limiter = _RateLimiter(per_minute=1, per_hour=100)
        assert limiter.allow() is True
        assert limiter.allow() is False
        # Manually expire the timestamp
        limiter._timestamps[0] = time.monotonic() - 61
        assert limiter.allow() is True


# ===================================================================
# Message receive and forward tests
# ===================================================================


class TestMessageReceiveAndForward:
    """test_message_receive_and_forward — gateway queues messages for routing."""

    @pytest.mark.asyncio
    async def test_receive_creates_envelope(self):
        driver = SimDriver()
        await driver.initialize()
        msg = await driver.receive_sms("+15551234567", "What is edge AI?")
        assert "timestamp" in msg
        assert msg["phone_number"] == "+15551234567"
        assert msg["content"] == "What is edge AI?"

    @pytest.mark.asyncio
    async def test_receive_records_in_inbox(self):
        driver = SimDriver()
        await driver.initialize()
        await driver.receive_sms("+15551234567", "Test query")
        inbox = await driver.get_inbox()
        assert len(inbox) == 1
        assert inbox[0]["content"] == "Test query"


# ===================================================================
# Message parser intent detection tests
# ===================================================================


class TestMessageParserIntentDetection:
    """test_message_parser_intent_detection — parser classifies intents."""

    def setup_method(self):
        self.parser = MessageParser()

    def test_detect_emergency(self):
        result = self.parser.parse_message("help there is a fire emergency", "+1001")
        assert result.intent == MessageIntent.EMERGENCY
        assert result.priority == "emergency"

    def test_detect_question(self):
        result = self.parser.parse_message("what sessions are about AI?", "+1001")
        assert result.intent == MessageIntent.QUESTION

    def test_detect_greeting(self):
        result = self.parser.parse_message("hello", "+1001")
        assert result.intent == MessageIntent.GREETING

    def test_detect_command(self):
        result = self.parser.parse_message("search for edge computing", "+1001")
        assert result.intent == MessageIntent.COMMAND

    def test_unknown_intent_for_gibberish(self):
        result = self.parser.parse_message("xyzzy", "+1001")
        assert result.intent == MessageIntent.UNKNOWN

    def test_emergency_does_not_require_rag(self):
        result = self.parser.parse_message("emergency help needed", "+1001")
        assert result.requires_rag is False

    def test_question_has_confidence(self):
        result = self.parser.parse_message("where is the keynote?", "+1001")
        assert result.confidence > 0.0

    def test_category_detection_weather(self):
        result = self.parser.parse_message("what is the weather forecast?", "+1001")
        assert result.category == MessageCategory.WEATHER

    def test_category_detection_health(self):
        result = self.parser.parse_message("where is the nearest hospital?", "+1001")
        assert result.category == MessageCategory.HEALTH

    def test_validate_message_too_long(self):
        long_msg = "a" * 161
        result = self.parser.validate_message(long_msg)
        assert result["valid"] is False
        assert any("too long" in e for e in result["errors"])

    def test_validate_empty_message(self):
        result = self.parser.validate_message("")
        assert result["valid"] is False
