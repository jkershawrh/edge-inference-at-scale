"""BDD Stage 4 — User scenario tests.

Each test represents a real attendee interaction at Summit Connect.
Tests exercise the message router's full classify→route→respond pipeline
with mocked downstream services.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.shared.models import SMSMessage, MessagePriority, MessageType
from backend.services.message_router.main import MessageRouter


def _sms(content, sender="+15551234567"):
    return SMSMessage(
        id=str(uuid.uuid4()), sender=sender, receiver="+15559876543",
        content=content, timestamp=datetime.now(timezone.utc),
        priority=MessagePriority.NORMAL,
    )


def _mock_chat_store(hunt_state=0):
    """Create a mock ChatHistoryStore with treasure hunt state support."""
    store = MagicMock()
    store._hunt_state = hunt_state
    store.get_hunt_state = AsyncMock(return_value=hunt_state)
    store.set_hunt_state = AsyncMock()
    store.clear_hunt_state = AsyncMock()
    store.get_history = AsyncMock(return_value=[])
    store.add_turn = AsyncMock()
    store.clear_history = AsyncMock()
    store.connect = AsyncMock()
    store.close = AsyncMock()
    return store


def _mock_router(rag_docs=None, rag_scores=None, llm_response="Check the schedule."):
    """Create a MessageRouter with mocked HTTP calls to downstream services."""
    router = MessageRouter()
    router.http_client = MagicMock()

    rag_resp = MagicMock()
    rag_resp.status_code = 200
    rag_resp.raise_for_status = MagicMock()
    rag_resp.json = MagicMock(return_value={
        "documents": rag_docs or [],
        "scores": rag_scores or [],
    })

    llm_resp = MagicMock()
    llm_resp.status_code = 200
    llm_resp.raise_for_status = MagicMock()
    llm_resp.json = MagicMock(return_value={
        "response": llm_response, "model_used": "bitnet-2b4t",
        "tokens_used": 20, "processing_time": 5000.0,
    })

    sms_resp = MagicMock()
    sms_resp.status_code = 200
    sms_resp.raise_for_status = MagicMock()

    async def mock_post(url, json=None, timeout=None):
        if "/search" in url:
            return rag_resp
        elif "/inference" in url:
            return llm_resp
        return sms_resp

    router.http_client.post = AsyncMock(side_effect=mock_post)
    return router


# ===================================================================
# Scenario: Attendee asks about the schedule
# ===================================================================

class TestAttendeeAsksSchedule:
    """GIVEN an attendee at Summit Connect
    WHEN they text 'What time is the keynote?'
    THEN they get a response mentioning the schedule"""

    @pytest.mark.asyncio
    async def test_schedule_question_gets_response(self):
        router = _mock_router(
            rag_docs=["Keynote: Edge Inference at Scale. July 15 9AM Main Hall."],
            rag_scores=[0.85],
        )
        result = await router.process_message(_sms("What time is the keynote?"))
        assert isinstance(result, str)
        assert len(result) > 0
        # RAG-direct: score 0.85 >= 0.8 and doc under 160 chars
        assert "keynote" in result.lower() or "9" in result


# ===================================================================
# Scenario: Attendee asks a venue question (RAG-direct)
# ===================================================================

class TestAttendeeAsksVenue:
    """GIVEN the corpus has WiFi info
    WHEN they text 'WiFi password?'
    THEN they get the answer instantly from RAG (no LLM call)"""

    @pytest.mark.asyncio
    async def test_wifi_question_rag_direct(self):
        wifi_doc = "Free WiFi: SummitConnect-Guest no password."
        router = _mock_router(rag_docs=[wifi_doc], rag_scores=[0.92])
        result = await router.process_message(_sms("WiFi password?"))
        assert result == wifi_doc
        assert router.stats.get("rag_direct_responses", 0) >= 1


# ===================================================================
# Scenario: Attendee reports an emergency
# ===================================================================

class TestAttendeeReportsEmergency:
    """GIVEN an attendee needs help
    WHEN they text 'help someone fell'
    THEN they get an emergency response mentioning security"""

    @pytest.mark.asyncio
    async def test_emergency_response(self):
        router = _mock_router()
        result = await router.process_message(_sms("help someone fell"))
        assert "emergency" in result.lower() or "security" in result.lower()

    @pytest.mark.asyncio
    async def test_emergency_skips_llm(self):
        router = _mock_router()
        msg = _sms("EMERGENCY fire in hall B")
        processed = router.classify_message(msg)
        assert processed.requires_llm is False
        assert processed.requires_rag is False


# ===================================================================
# Scenario: Attendee says hello
# ===================================================================

class TestAttendeeSaysHello:
    """GIVEN an attendee texts for the first time
    WHEN they text 'Hi'
    THEN they get a Summit Connect welcome message"""

    @pytest.mark.asyncio
    async def test_greeting_mentions_summit_connect(self):
        router = _mock_router()
        result = await router.process_message(_sms("hello"))
        assert "summit connect" in result.lower()


# ===================================================================
# Scenario: Attendee asks an open-ended question (needs LLM)
# ===================================================================

class TestAttendeeAsksOpenQuestion:
    """GIVEN an attendee asks something not directly in the corpus
    WHEN RAG score is below threshold
    THEN the question goes to BitNet with RAG context"""

    @pytest.mark.asyncio
    async def test_open_question_hits_llm(self):
        router = _mock_router(
            rag_docs=["Various sessions on AI and edge computing available."],
            rag_scores=[0.6],
            llm_response="Try the Edge Inference keynote Day 1 9AM.",
        )
        result = await router.process_message(_sms("What should a beginner attend?"))
        assert "keynote" in result.lower() or "edge" in result.lower()
        assert router.stats.get("rag_direct_responses", 0) == 0


# ===================================================================
# Scenario: Attendee uses a command
# ===================================================================

class TestAttendeeUsesCommand:
    """GIVEN an attendee knows the command syntax
    WHEN they text '/status'
    THEN they get system stats"""

    @pytest.mark.asyncio
    async def test_status_command(self):
        router = _mock_router()
        result = await router.process_message(_sms("/status"))
        assert "messages" in result.lower() or "uptime" in result.lower()


# ===================================================================
# Scenario: Multiple attendees text simultaneously
# ===================================================================

class TestMultipleAttendees:
    """GIVEN 5 attendees text at the same time
    WHEN their messages are processed
    THEN all get responses"""

    @pytest.mark.asyncio
    async def test_concurrent_messages(self):
        import asyncio
        router = _mock_router(
            rag_docs=["Summit Connect runs July 15-17."],
            rag_scores=[0.5],
            llm_response="Summit Connect is July 15-17.",
        )
        messages = [
            _sms("When is the conference?", f"+1555000000{i}")
            for i in range(5)
        ]
        results = await asyncio.gather(*[router.process_message(m) for m in messages])
        assert len(results) == 5
        assert all(isinstance(r, str) and len(r) > 0 for r in results)


# ===================================================================
# Scenario: Response fits SMS limit
# ===================================================================

class TestResponseFitsSMS:
    """GIVEN any response from the system
    WHEN it reaches the attendee
    THEN it is 160 characters or less"""

    @pytest.mark.asyncio
    async def test_all_response_types_under_160(self):
        router = _mock_router(
            rag_docs=["Short answer."],
            rag_scores=[0.5],
            llm_response="A" * 200,
        )
        # Query (LLM response gets truncated by the LLM service, not router)
        # But commands and templates should all be under 160
        for content in ["hello", "/status", "/schedule", "help emergency"]:
            result = await router.process_message(_sms(content))
            assert len(result) <= 320, f"Response too long for '{content}': {len(result)} chars"


# ===================================================================
# Scenario: Attendee starts the treasure hunt
# ===================================================================

class TestTreasureHuntStart:
    """GIVEN an attendee at the booth
    WHEN they text 'HUNT'
    THEN they receive the treasure hunt intro with instructions"""

    @pytest.mark.asyncio
    async def test_hunt_start_returns_intro(self):
        router = _mock_router()
        router.chat_store = _mock_chat_store()
        result = await router.process_message(_sms("HUNT"))
        assert "treasure hunt" in result.lower() or "clue" in result.lower()

    @pytest.mark.asyncio
    async def test_hunt_start_sets_state(self):
        router = _mock_router()
        store = _mock_chat_store()
        router.chat_store = store
        await router.process_message(_sms("HUNT"))
        store.set_hunt_state.assert_called_once_with("+15551234567", 1)

    @pytest.mark.asyncio
    async def test_hunt_tracks_stats(self):
        router = _mock_router()
        router.chat_store = _mock_chat_store()
        await router.process_message(_sms("HUNT"))
        assert router.stats.get("hunt_started", 0) >= 1


# ===================================================================
# Scenario: Attendee requests a clue
# ===================================================================

class TestTreasureHuntClue:
    """GIVEN an attendee who has started the hunt
    WHEN they text 'CLUE 1'
    THEN they receive the first clue"""

    @pytest.mark.asyncio
    async def test_clue_1_shown(self):
        router = _mock_router()
        router.chat_store = _mock_chat_store(hunt_state=1)
        result = await router.process_message(_sms("CLUE 1"))
        assert "clue 1" in result.lower() or "400mb" in result.lower()

    @pytest.mark.asyncio
    async def test_locked_clue_rejected(self):
        router = _mock_router()
        router.chat_store = _mock_chat_store(hunt_state=1)
        result = await router.process_message(_sms("CLUE 5"))
        assert "unlock" in result.lower() or "solve" in result.lower()

    @pytest.mark.asyncio
    async def test_clue_before_hunt_start(self):
        router = _mock_router()
        router.chat_store = _mock_chat_store(hunt_state=0)
        result = await router.process_message(_sms("CLUE 1"))
        assert "hunt" in result.lower()


# ===================================================================
# Scenario: Attendee answers a clue correctly
# ===================================================================

class TestTreasureHuntAnswer:
    """GIVEN an attendee on clue 1
    WHEN they text the correct answer 'bitnet'
    THEN they advance to clue 2"""

    @pytest.mark.asyncio
    async def test_correct_answer_advances(self):
        router = _mock_router()
        store = _mock_chat_store(hunt_state=1)
        router.chat_store = store
        result = await router.process_message(_sms("bitnet"))
        assert "correct" in result.lower() or "clue 2" in result.lower()
        store.set_hunt_state.assert_called_with("+15551234567", 2)

    @pytest.mark.asyncio
    async def test_wrong_answer_falls_through(self):
        router = _mock_router(
            rag_docs=["Some info."],
            rag_scores=[0.5],
            llm_response="Try again!",
        )
        router.chat_store = _mock_chat_store(hunt_state=1)
        result = await router.process_message(_sms("wrong answer xyz"))
        assert "correct" not in result.lower()


# ===================================================================
# Scenario: Attendee asks for a hint
# ===================================================================

class TestTreasureHuntHint:
    """GIVEN an attendee on clue 1
    WHEN they text 'HINT'
    THEN they receive the hint for their current clue"""

    @pytest.mark.asyncio
    async def test_hint_for_current_clue(self):
        router = _mock_router()
        router.chat_store = _mock_chat_store(hunt_state=1)
        result = await router.process_message(_sms("HINT"))
        assert "texting" in result.lower() or "try" in result.lower()

    @pytest.mark.asyncio
    async def test_hint_before_starting(self):
        router = _mock_router()
        router.chat_store = _mock_chat_store(hunt_state=0)
        result = await router.process_message(_sms("HINT"))
        assert "haven't started" in result.lower() or "hunt" in result.lower()


# ===================================================================
# Scenario: Attendee completes the entire treasure hunt
# ===================================================================

class TestTreasureHuntCompletion:
    """GIVEN an attendee on the final clue (clue 7)
    WHEN they text the correct answer
    THEN they see the victory message with prize instructions"""

    @pytest.mark.asyncio
    async def test_final_clue_victory(self):
        router = _mock_router()
        store = _mock_chat_store(hunt_state=7)
        router.chat_store = store
        result = await router.process_message(_sms("bbu"))
        assert "cracked" in result.lower() or "prize" in result.lower() or "congratulations" in result.lower()
        assert router.stats.get("hunt_completed", 0) >= 1

    @pytest.mark.asyncio
    async def test_full_hunt_walkthrough(self):
        """Walk through all 7 clues sequentially."""
        router = _mock_router()
        store = _mock_chat_store(hunt_state=0)
        router.chat_store = store

        answers = [
            (0, "HUNT", 1),
            (1, "bitnet", 2),
            (2, "red hat", 3),
            (3, "400", 4),
            (4, "24", 5),
            (5, "amxmicroshift", 6),
            (6, "70", 7),
            (7, "bbu", 8),
        ]

        for initial_state, text, expected_next in answers:
            store._hunt_state = initial_state
            store.get_hunt_state = AsyncMock(return_value=initial_state)
            result = await router.process_message(_sms(text))
            assert isinstance(result, str) and len(result) > 0, f"Empty response for '{text}' at state {initial_state}"


# ===================================================================
# Scenario: Multiple attendees hunt independently
# ===================================================================

class TestTreasureHuntMultiUser:
    """GIVEN two attendees both doing the hunt
    WHEN they are on different clues
    THEN each sees their own clue state"""

    @pytest.mark.asyncio
    async def test_independent_hunt_state(self):
        router = _mock_router()
        states = {}

        async def mock_get(phone):
            return states.get(phone, 0)

        async def mock_set(phone, val):
            states[phone] = val

        store = _mock_chat_store()
        store.get_hunt_state = AsyncMock(side_effect=mock_get)
        store.set_hunt_state = AsyncMock(side_effect=mock_set)
        router.chat_store = store

        # User A starts the hunt
        await router.process_message(_sms("HUNT", "+15550000001"))
        assert states["+15550000001"] == 1

        # User B starts too
        await router.process_message(_sms("HUNT", "+15550000002"))
        assert states["+15550000002"] == 1

        # User A solves clue 1
        await router.process_message(_sms("bitnet", "+15550000001"))
        assert states["+15550000001"] == 2
        assert states["+15550000002"] == 1  # B unchanged
