"""Tests for signed Discord interaction conversion and response delivery."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.requests import Request

from backend.api_gateway.main import (
    _complete_discord_interaction,
    discord_interactions,
    gateway,
)
from backend.shared.config import settings
from backend.services.discord_gateway.adapter import (
    DISCORD_MAX_RESPONSE_CHARS,
    edit_original_response,
    extract_command_query,
    interaction_to_channel_message,
    verify_interaction_signature,
)
from backend.shared.models import MessageChannel


def _signed_payload():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    payload = {
        "id": "interaction-1",
        "application_id": "app-1",
        "type": 2,
        "token": "response-token",
        "channel_id": "channel-1",
        "guild_id": "guild-1",
        "member": {"user": {"id": "user-1"}},
        "data": {
            "name": "ask",
            "options": [{"name": "question", "type": 3, "value": "Where is shelter?"}],
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = "1700000000"
    signature = private_key.sign(timestamp.encode("utf-8") + raw_body).hex()
    return payload, raw_body, timestamp, signature, public_key


def _request(raw_body: bytes, timestamp: str, signature: str) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/discord/interactions",
            "raw_path": b"/discord/interactions",
            "query_string": b"",
            "headers": [
                (b"x-signature-ed25519", signature.encode("ascii")),
                (b"x-signature-timestamp", timestamp.encode("ascii")),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        },
        receive,
    )


def test_valid_discord_signature_is_accepted():
    _, raw_body, timestamp, signature, public_key = _signed_payload()
    assert verify_interaction_signature(public_key, signature, timestamp, raw_body)


def test_tampered_discord_body_is_rejected():
    _, raw_body, timestamp, signature, public_key = _signed_payload()
    assert not verify_interaction_signature(
        public_key, signature, timestamp, raw_body + b" "
    )


@pytest.mark.asyncio
async def test_signed_ping_endpoint_returns_pong(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    raw_body = b'{"type":1}'
    timestamp = "1700000000"
    signature = private_key.sign(timestamp.encode("utf-8") + raw_body).hex()
    monkeypatch.setattr(settings, "discord_public_key", public_key)

    result = await discord_interactions(
        _request(raw_body, timestamp, signature), BackgroundTasks()
    )

    assert result == {"type": 1}


@pytest.mark.asyncio
async def test_invalid_signature_endpoint_returns_401(monkeypatch):
    _, raw_body, timestamp, _, public_key = _signed_payload()
    monkeypatch.setattr(settings, "discord_public_key", public_key)

    with pytest.raises(HTTPException) as exc:
        await discord_interactions(
            _request(raw_body, timestamp, "00" * 64), BackgroundTasks()
        )

    assert exc.value.status_code == 401


def test_slash_command_converts_to_channel_message():
    payload, _, _, _, _ = _signed_payload()
    query = extract_command_query(payload)
    message = interaction_to_channel_message(payload, query)

    assert query == "Where is shelter?"
    assert message.channel == MessageChannel.DISCORD
    assert message.sender == "discord:user-1"
    assert message.receiver == "discord:channel-1"
    assert "token" not in message.metadata


@pytest.mark.asyncio
async def test_deferred_response_is_edited_without_mentions():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"id": "message-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await edit_original_response(
            client,
            "https://discord.com/api/v10",
            "app-1",
            "secret-token",
            "x" * (DISCORD_MAX_RESPONSE_CHARS + 20),
        )

    assert captured["path"].endswith(
        "/webhooks/app-1/secret-token/messages/@original"
    )
    assert len(captured["body"]["content"]) == DISCORD_MAX_RESPONSE_CHARS
    assert captured["body"]["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_background_flow_routes_shared_envelope_and_edits_discord(monkeypatch):
    payload, _, _, _, _ = _signed_payload()
    routed = AsyncMock(return_value={"response": "Shelter is at the school."})
    monkeypatch.setattr(gateway, "proxy", routed)
    monkeypatch.setattr(settings, "discord_api_base", "https://discord.test/api/v10")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"id": "message-1"})

    previous_client = gateway.client
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway.client = client
            await _complete_discord_interaction(payload, "Where is shelter?")
    finally:
        gateway.client = previous_client

    routed_payload = routed.await_args.args[3]
    assert routed_payload["channel"] == "discord"
    assert routed_payload["content"] == "Where is shelter?"
    assert captured["body"]["content"] == "Shelter is at the school."
    assert captured["path"].endswith(
        "/webhooks/app-1/response-token/messages/@original"
    )
