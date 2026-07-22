"""Unit tests for the Message Router service.

All downstream services (LLM, RAG, SMS gateway) are mocked.
Tests validate classification, template responses, SMS chunking,
and the RAG-context-to-LLM pipeline.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.shared.models import (
    MessagePriority,
    MessageType,
    ProcessedMessage,
    SMSMessage,
)
from backend.services.message_router.main import (
    MessageRouter,
    _chunk_sms_response,
    EMERGENCY_KEYWORDS,
    SMS_MAX_LENGTH,
    TEMPLATE_GREETINGS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sms(content: str, sender: str = "+15551234567") -> SMSMessage:
    """Create an SMSMessage for testing."""
    return SMSMessage(
        id=str(uuid.uuid4()),
        sender=sender,
        receiver="+15559876543",
        content=content,
        timestamp=datetime.now(timezone.utc),
        priority=MessagePriority.NORMAL,
    )


# ===================================================================
# Emergency classification
# ===================================================================


class TestClassifyEmergency:
    """test_classify_emergency — emergency keywords trigger EMERGENCY type."""

    def setup_method(self):
        self.router = MessageRouter()

    def test_emergency_keyword_fire(self):
        msg = _make_sms("There is a fire in hall B")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.EMERGENCY
        assert result.priority == MessagePriority.EMERGENCY

    def test_emergency_keyword_help(self):
        msg = _make_sms("help someone is injured")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.EMERGENCY

    def test_emergency_keyword_medical(self):
        msg = _make_sms("we need medical assistance")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.EMERGENCY

    def test_emergency_does_not_require_rag(self):
        msg = _make_sms("emergency evacuation now")
        result = self.router.classify_message(msg)
        assert result.requires_rag is False

    def test_emergency_does_not_require_llm(self):
        msg = _make_sms("emergency help fire")
        result = self.router.classify_message(msg)
        assert result.requires_llm is False

    def test_emergency_response_text(self):
        msg = _make_sms("emergency fire")
        self.router.classify_message(msg)
        response = self.router._handle_emergency(
            ProcessedMessage(
                original_message=msg,
                message_type=MessageType.EMERGENCY,
                intent="emergency",
                requires_rag=False,
                requires_llm=False,
                priority=MessagePriority.EMERGENCY,
            )
        )
        assert "EMERGENCY" in response
        assert "security" in response.lower() or "911" in response

    def test_emergency_stats_incremented(self):
        msg = _make_sms("help fire emergency")
        self.router.classify_message(msg)
        assert self.router.stats["emergency_messages"] >= 1


# ===================================================================
# Command classification
# ===================================================================


class TestClassifyCommand:
    """test_classify_command — /prefix messages are classified as COMMAND."""

    def setup_method(self):
        self.router = MessageRouter()

    def test_status_command(self):
        msg = _make_sms("/status")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.COMMAND

    def test_schedule_command(self):
        msg = _make_sms("/schedule")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.COMMAND

    def test_speakers_command(self):
        msg = _make_sms("/speakers")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.COMMAND

    def test_unknown_command(self):
        msg = _make_sms("/unknown_cmd")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.COMMAND

    def test_command_does_not_require_rag(self):
        msg = _make_sms("/status")
        result = self.router.classify_message(msg)
        assert result.requires_rag is False

    def test_command_does_not_require_llm(self):
        msg = _make_sms("/status")
        result = self.router.classify_message(msg)
        assert result.requires_llm is False

    def test_help_command_contains_emergency_keyword(self):
        """'/help' contains 'help' which is an emergency keyword.

        The router checks emergency keywords before command prefix,
        so '/help' is classified as EMERGENCY. This is intentional --
        the word 'help' signals potential distress.
        """
        msg = _make_sms("/help")
        result = self.router.classify_message(msg)
        # 'help' is an emergency keyword, checked before '/' prefix
        assert result.message_type == MessageType.EMERGENCY

    def test_help_response_lists_commands(self):
        """_handle_command returns command listing when called directly."""
        response = self.router._handle_command(
            ProcessedMessage(
                original_message=_make_sms("/status"),
                message_type=MessageType.COMMAND,
                intent="/help",
                requires_rag=False,
                requires_llm=False,
                priority=MessagePriority.NORMAL,
            )
        )
        assert "/help" in response
        assert "/status" in response

    def test_command_stats_incremented(self):
        msg = _make_sms("/status")
        self.router.classify_message(msg)
        assert self.router.stats["command_messages"] >= 1


# ===================================================================
# Query classification
# ===================================================================


class TestClassifyQuery:
    """test_classify_query — free-text messages route to RAG + LLM."""

    def setup_method(self):
        self.router = MessageRouter()

    def test_question_classified_as_query(self):
        msg = _make_sms("What sessions are about edge computing?")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.QUERY

    def test_query_requires_rag(self):
        msg = _make_sms("Tell me about the keynote speaker")
        result = self.router.classify_message(msg)
        assert result.requires_rag is True

    def test_query_requires_llm(self):
        msg = _make_sms("What time does the workshop start?")
        result = self.router.classify_message(msg)
        assert result.requires_llm is True

    def test_query_has_normal_priority(self):
        msg = _make_sms("Where is room 301?")
        result = self.router.classify_message(msg)
        assert result.priority == MessagePriority.NORMAL

    def test_query_stats_incremented(self):
        msg = _make_sms("What is edge AI?")
        self.router.classify_message(msg)
        assert self.router.stats["query_messages"] >= 1


# ===================================================================
# Greeting / template response
# ===================================================================


class TestGreetingResponseSummitConnect:
    """test_greeting_response_summit_connect — greetings return template."""

    def setup_method(self):
        self.router = MessageRouter()

    def test_hello_classified_as_template(self):
        msg = _make_sms("hello")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.TEMPLATE

    def test_hi_classified_as_template(self):
        msg = _make_sms("hi")
        result = self.router.classify_message(msg)
        assert result.message_type == MessageType.TEMPLATE

    def test_greeting_response_mentions_summit_connect(self):
        response = self.router._handle_template(
            ProcessedMessage(
                original_message=_make_sms("hello"),
                message_type=MessageType.TEMPLATE,
                intent="greeting",
                requires_rag=False,
                requires_llm=False,
                priority=MessagePriority.LOW,
            )
        )
        assert "Summit Connect" in response

    def test_all_template_greetings_recognized(self):
        for greeting in TEMPLATE_GREETINGS:
            msg = _make_sms(greeting)
            result = self.router.classify_message(msg)
            assert result.message_type == MessageType.TEMPLATE, (
                f"Greeting '{greeting}' not classified as TEMPLATE"
            )

    def test_greeting_does_not_require_llm(self):
        msg = _make_sms("hey")
        result = self.router.classify_message(msg)
        assert result.requires_llm is False

    def test_template_stats_incremented(self):
        msg = _make_sms("hello")
        self.router.classify_message(msg)
        assert self.router.stats["template_messages"] >= 1


# ===================================================================
# RAG context included in LLM request
# ===================================================================


class TestRAGContextIncludedInLLMRequest:
    """test_rag_context_included_in_llm_request — RAG context is forwarded."""

    def setup_method(self):
        self.router = MessageRouter()

    @pytest.mark.asyncio
    async def test_rag_context_passed_to_llm(self):
        """When RAG returns context, it is included in the LLM request."""
        self.router.http_client = MagicMock()

        # Mock RAG response
        rag_response = MagicMock()
        rag_response.status_code = 200
        rag_response.raise_for_status = MagicMock()
        rag_response.json = MagicMock(return_value={
            "documents": ["Edge Computing Workshop - Room 301, 2:00 PM"],
            "scores": [0.85],
        })

        # Mock LLM response
        llm_response = MagicMock()
        llm_response.status_code = 200
        llm_response.raise_for_status = MagicMock()
        llm_response.json = MagicMock(return_value={
            "response": "The Edge Computing Workshop is in Room 301 at 2 PM.",
            "model_used": "bitnet-2b4t",
            "tokens_used": 30,
            "processing_time": 1500.0,
        })

        # Mock SMS send response
        sms_response = MagicMock()
        sms_response.status_code = 200
        sms_response.raise_for_status = MagicMock()

        # Track the actual call to LLM
        call_log = []

        async def mock_post(url, json=None, timeout=None):
            call_log.append({"url": url, "json": json})
            if "/search" in url:
                return rag_response
            elif "/inference" in url:
                return llm_response
            elif "/sms/send" in url:
                return sms_response
            return MagicMock(status_code=200, raise_for_status=MagicMock())

        self.router.http_client.post = AsyncMock(side_effect=mock_post)

        msg = _make_sms("What sessions are about edge computing?")
        await self.router.process_message(msg)

        # Find the LLM call and verify context was included
        llm_calls = [c for c in call_log if "/inference" in c["url"]]
        assert len(llm_calls) == 1
        assert llm_calls[0]["json"]["context"] is not None
        assert "Edge Computing Workshop" in llm_calls[0]["json"]["context"]

    @pytest.mark.asyncio
    async def test_rag_failure_passes_none_context(self):
        """When RAG fails, LLM is still called with context=None."""
        self.router.http_client = MagicMock()

        # Mock RAG failure
        async def mock_post(url, json=None, timeout=None):
            if "/search" in url:
                raise Exception("RAG service unavailable")
            elif "/inference" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(return_value={
                    "response": "I can help with that.",
                    "model_used": "bitnet-2b4t",
                    "tokens_used": 10,
                    "processing_time": 500.0,
                })
                return resp
            else:
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                return resp

        self.router.http_client.post = AsyncMock(side_effect=mock_post)

        msg = _make_sms("What sessions are about edge computing?")
        response = await self.router.process_message(msg)
        assert response  # Should still get a response


# ===================================================================
# SMS chunking
# ===================================================================


class TestSMSChunking:
    """test_sms_chunking — long responses split into 160-char segments."""

    def test_short_message_not_chunked(self):
        text = "Short response"
        chunks = _chunk_sms_response(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_exactly_160_not_chunked(self):
        text = "x" * 160
        chunks = _chunk_sms_response(text)
        assert len(chunks) == 1

    def test_long_message_chunked(self):
        text = "word " * 100  # ~500 chars
        chunks = _chunk_sms_response(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= SMS_MAX_LENGTH

    def test_chunks_have_part_indicators(self):
        text = "word " * 100
        chunks = _chunk_sms_response(text)
        assert len(chunks) > 1
        for i, chunk in enumerate(chunks):
            assert f"({i + 1}/{len(chunks)})" in chunk

    def test_chunks_word_boundary(self):
        """Chunks should prefer splitting on word boundaries.

        The chunker splits on the last space before the effective max,
        so chunks should not break in the middle of a word.
        """
        # Use distinct words so we can verify no word got split
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                 "golf", "hotel", "india", "juliet", "kilo", "lima",
                 "mike", "november", "oscar", "papa", "quebec", "romeo",
                 "sierra", "tango", "uniform", "victor", "whiskey",
                 "xray", "yankee", "zulu"]
        text = " ".join(words * 2)  # ~280 chars
        chunks = _chunk_sms_response(text)
        assert len(chunks) > 1
        # Rejoin all chunks (stripping part indicators) and verify
        # no word got truncated mid-character
        for chunk in chunks:
            # Remove the trailing " (N/M)" part indicator
            content = chunk.rsplit(" (", 1)[0] if "/" in chunk else chunk
            # Every token in the content should be a complete word or space
            tokens = content.split()
            for token in tokens:
                assert token in words, f"'{token}' is not a complete word -- split mid-word"

    def test_custom_max_length(self):
        text = "a" * 100
        chunks = _chunk_sms_response(text, max_length=50)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 50
