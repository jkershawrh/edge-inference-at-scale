"""SMS input edge case tests — unicode, special chars, boundary lengths."""

from backend.services.sms_gateway.message_parser import MessageParser, MessageIntent


class TestUnicodeInputs:
    def setup_method(self):
        self.parser = MessageParser()

    def test_emoji_message(self):
        result = self.parser.parse_message("🔥 Where's the party? 🎉", "+1234567890")
        assert result is not None
        assert result.intent in (MessageIntent.QUESTION, MessageIntent.UNKNOWN)

    def test_cjk_characters(self):
        result = self.parser.parse_message("会议在哪里?", "+1234567890")
        assert result is not None

    def test_arabic_text(self):
        result = self.parser.parse_message("أين المؤتمر؟", "+1234567890")
        assert result is not None

    def test_mixed_emoji_and_text(self):
        result = self.parser.parse_message("👋 Hello! Where is room A1? 📍", "+1234567890")
        assert result is not None


class TestSpecialCharInputs:
    def setup_method(self):
        self.parser = MessageParser()

    def test_html_tags(self):
        result = self.parser.parse_message("<script>alert(1)</script>", "+1234567890")
        assert result is not None

    def test_sql_injection(self):
        result = self.parser.parse_message("'; DROP TABLE messages; --", "+1234567890")
        assert result is not None

    def test_null_bytes(self):
        result = self.parser.parse_message("Hello\x00World", "+1234567890")
        assert result is not None

    def test_special_chars_only(self):
        result = self.parser.parse_message("!@#$%^&*()", "+1234567890")
        assert result is not None
        assert result.intent == MessageIntent.UNKNOWN

    def test_newlines(self):
        result = self.parser.parse_message("Line1\nLine2\nLine3", "+1234567890")
        assert result is not None


class TestWhitespaceInputs:
    """FINDING: MessageParser.validate_message does NOT reject whitespace-only
    messages. This is a known gap — whitespace passes validation but produces
    UNKNOWN intent with low confidence. For the demo, this is acceptable since
    the LLM will handle it, but it should be fixed for production."""

    def setup_method(self):
        self.parser = MessageParser()

    def test_whitespace_only_passes_validation(self):
        validation = self.parser.validate_message("   ")
        # Currently passes — parser doesn't strip/check whitespace
        assert validation["valid"] is True

    def test_whitespace_only_gets_unknown_intent(self):
        result = self.parser.parse_message("   ", "+1234567890")
        assert result.intent == MessageIntent.UNKNOWN


class TestBoundaryLengths:
    def setup_method(self):
        self.parser = MessageParser()

    def test_exactly_160_chars(self):
        msg = "a" * 160
        validation = self.parser.validate_message(msg)
        assert not validation["errors"]

    def test_161_chars_invalid(self):
        msg = "a" * 161
        validation = self.parser.validate_message(msg)
        assert validation["warnings"] or validation["errors"]

    def test_single_char_question(self):
        result = self.parser.parse_message("?", "+1234567890")
        assert result is not None
        assert result.intent == MessageIntent.QUESTION

    def test_single_char_letter(self):
        result = self.parser.parse_message("a", "+1234567890")
        assert result is not None

    def test_numbers_only(self):
        result = self.parser.parse_message("12345", "+1234567890")
        assert result is not None
        assert result.intent in (MessageIntent.UNKNOWN, MessageIntent.INFORMATION)

    def test_empty_string_validation(self):
        validation = self.parser.validate_message("")
        assert validation["errors"]
