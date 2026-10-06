"""Tests for the channel-neutral message envelope."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from backend.services.message_router.main import MessageRouter
from backend.shared.models import ChannelMessage, MessageChannel, SMSMessage


def test_channel_message_allows_non_sms_payload_length():
    message = ChannelMessage(
        sender="discord:user-1",
        receiver="discord:channel-1",
        content="q" * 1000,
        channel=MessageChannel.DISCORD,
    )
    assert len(message.content) == 1000


def test_sms_message_keeps_160_character_limit():
    with pytest.raises(ValidationError):
        SMSMessage(sender="+15550001", receiver="+15550002", content="q" * 161)


@pytest.mark.asyncio
async def test_discord_response_is_returned_without_sms_delivery():
    router = MessageRouter()
    router.http_client = MagicMock()
    privacy_response = MagicMock(status_code=200)
    privacy_response.json = MagicMock(return_value={"valid": True})
    router.http_client.post = AsyncMock(return_value=privacy_response)

    response = await router.process_message(
        ChannelMessage(
            sender="discord:user-1",
            receiver="discord:channel-1",
            content="hello",
            channel=MessageChannel.DISCORD,
        )
    )

    assert response.startswith("Welcome to Summit Connect")
    called_urls = [call.args[0] for call in router.http_client.post.await_args_list]
    assert not any("/sms/send" in url for url in called_urls)
    assert router.stats["discord_responses"] == 1
