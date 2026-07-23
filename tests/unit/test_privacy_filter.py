"""Privacy filter tests — PII detection, rate limiting, user blocking."""

import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.shared.models import SMSMessage, MessagePriority


# Patch the global PrivacyFilter data_dir before importing the module
_tmpdir = tempfile.mkdtemp()
with patch("pathlib.Path.mkdir"):
    from backend.services.privacy_filter.main import PrivacyFilter, DataCategory


def _make_filter():
    """Create a PrivacyFilter with a writable temp data dir."""
    tmpdir = tempfile.mkdtemp()
    with patch("pathlib.Path.mkdir"):
        pf = PrivacyFilter()
    pf.data_dir = Path(tmpdir)
    pf.consent_file = pf.data_dir / "consent_records.json"
    pf.audit_file = pf.data_dir / "audit_log.json"
    pf.blocklist_file = pf.data_dir / "blocklist.json"
    return pf


class TestPIIDetection:
    def setup_method(self):
        self.pf = _make_filter()

    def test_detect_phone_number(self):
        result = self.pf.sanitize_text("Call 555-123-4567 for info")
        assert result["has_sensitive_data"] is True
        types = [d["type"] for d in result["detected_patterns"]]
        assert "phone" in types

    def test_detect_email(self):
        result = self.pf.sanitize_text("Email me at user@example.com")
        assert result["has_sensitive_data"] is True
        types = [d["type"] for d in result["detected_patterns"]]
        assert "email" in types

    def test_detect_ssn(self):
        result = self.pf.sanitize_text("My SSN is 123-45-6789")
        assert result["has_sensitive_data"] is True
        types = [d["type"] for d in result["detected_patterns"]]
        assert "ssn" in types

    def test_detect_credit_card(self):
        result = self.pf.sanitize_text("Card: 4111 1111 1111 1111")
        assert result["has_sensitive_data"] is True
        types = [d["type"] for d in result["detected_patterns"]]
        assert "credit_card" in types

    def test_no_false_positive(self):
        result = self.pf.sanitize_text("Summit Connect is a great conference about edge AI")
        assert result["has_sensitive_data"] is False
        assert result["risk_level"] == "low"

    def test_redact_replaces_pii(self):
        result = self.pf.sanitize_text("Call 555-123-4567", redact=True)
        assert "[PHONE_REDACTED]" in result["sanitized_text"]
        assert "555-123-4567" not in result["sanitized_text"]


class TestRateLimiting:
    def setup_method(self):
        self.pf = _make_filter()

    def test_allows_within_limit(self):
        for _ in range(5):
            self.pf.record_message("+1234567890")
        result = self.pf.check_rate_limit("+1234567890")
        assert result["within_limits"] is True

    def test_blocks_after_minute_limit(self):
        for _ in range(15):
            self.pf.record_message("+1234567890")
        result = self.pf.check_rate_limit("+1234567890")
        assert result["within_limits"] is False


class TestUserBlocking:
    def setup_method(self):
        self.pf = _make_filter()

    def test_block_user(self):
        self.pf.block_user("+1234567890")
        assert self.pf.is_user_blocked("+1234567890") is True

    def test_unblock_user(self):
        self.pf.block_user("+1234567890")
        self.pf.unblock_user("+1234567890")
        assert self.pf.is_user_blocked("+1234567890") is False

    def test_blocked_user_message_rejected(self):
        self.pf.block_user("+1234567890")
        msg = SMSMessage(
            sender="+1234567890", receiver="+0000000000", content="hello",
            timestamp=datetime.now(timezone.utc), priority=MessagePriority.NORMAL,
        )
        result = self.pf.validate_message(msg)
        assert result["valid"] is False
        assert result["reason"] == "user_blocked"
