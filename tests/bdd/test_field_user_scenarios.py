"""BDD scenarios for the disconnected Lil EVY field mission.

These scenarios use synthetic guidance. They validate product behavior, not the
factual suitability of a real emergency corpus or a physical radio path.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.message_router.main import MessageRouter
from backend.shared.config import settings
from backend.shared.models import ChannelMessage, MessageChannel


def _router() -> MessageRouter:
    router = MessageRouter()
    router.http_client = None
    router.chat_store = MagicMock()
    router.chat_store.get_history = AsyncMock(return_value=[])
    router.chat_store.add_turn = AsyncMock()
    router.send_response = AsyncMock(return_value=True)
    router.route_to_llm = AsyncMock(return_value="generated response")
    return router


def _message(content: str, channel: MessageChannel = MessageChannel.SMS):
    return ChannelMessage(
        sender=f"{channel.value}:resident-1",
        receiver=f"{channel.value}:lil-evy",
        content=content,
        channel=channel,
    )


class TestResidentGetsSupportedLocalAnswer:
    """GIVEN approved local evidence, WHEN a resident asks, THEN evidence answers."""

    @pytest.mark.asyncio
    async def test_high_confidence_evidence_answers_without_generation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "rag_grounding_required", True)
        router = _router()
        guidance = "Safe water is available at Ridge School from 08:00 to 18:00."
        router.route_to_rag = AsyncMock(return_value=(guidance, 0.98, guidance))

        response = await router.process_message(_message("Where is safe water?"))

        assert response == guidance
        router.route_to_llm.assert_not_awaited()


class TestResidentGetsSafeNoEvidenceResponse:
    """GIVEN no local evidence, WHEN a resident asks, THEN Lil EVY refuses safely."""

    @pytest.mark.asyncio
    async def test_missing_evidence_never_falls_back_to_generation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "rag_grounding_required", True)
        router = _router()
        router.route_to_rag = AsyncMock(return_value=(None, 0.0, None))

        response = await router.process_message(_message("Is the east bridge safe?"))

        assert response == settings.grounding_failure_message
        router.route_to_llm.assert_not_awaited()


class TestResidentEmergencyGuidance:
    """GIVEN approved emergency evidence, WHEN danger is reported, THEN it is used directly."""

    @pytest.mark.asyncio
    async def test_emergency_guidance_bypasses_the_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "rag_grounding_required", True)
        monkeypatch.setattr(settings, "emergency_rag_enabled", True)
        router = _router()
        guidance = "Flood route: follow blue markers north. Never cross moving water."
        router.route_to_rag = AsyncMock(return_value=(guidance, 0.99, guidance))

        response = await router.process_message(
            _message("Emergency flooding: where do we evacuate?")
        )

        assert response == guidance
        router.route_to_llm.assert_not_awaited()


class TestResidentUsesAvailableTransport:
    """GIVEN one field core, WHEN transport changes, THEN the answer policy does not."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "channel",
        [MessageChannel.SMS, MessageChannel.DISCORD, MessageChannel.LORA],
    )
    async def test_supported_answer_is_channel_neutral(
        self, monkeypatch: pytest.MonkeyPatch, channel: MessageChannel
    ) -> None:
        monkeypatch.setattr(settings, "rag_grounding_required", True)
        router = _router()
        guidance = "The local clinic is beside Ridge School."
        router.route_to_rag = AsyncMock(return_value=(guidance, 0.97, guidance))

        response = await router.process_message(_message("Where is the clinic?", channel))

        assert response == guidance
        router.route_to_llm.assert_not_awaited()
