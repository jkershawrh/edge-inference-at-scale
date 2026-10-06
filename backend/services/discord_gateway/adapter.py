"""Secure Discord slash-command adapter.

Discord is a development transport, not a dependency of the offline field
architecture.  It converts signed ``/ask`` interactions into the same shared
message envelope used by SMS, then edits Discord's deferred response with the
pipeline result.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from backend.shared.models import ChannelMessage, MessageChannel


DISCORD_PING = 1
DISCORD_APPLICATION_COMMAND = 2
DISCORD_PONG = 1
DISCORD_MESSAGE = 4
DISCORD_DEFERRED_MESSAGE = 5
DISCORD_EPHEMERAL_FLAG = 64
DISCORD_MAX_RESPONSE_CHARS = 2000


def verify_interaction_signature(
    public_key_hex: str,
    signature_hex: str,
    timestamp: str,
    raw_body: bytes,
) -> bool:
    """Verify Discord's Ed25519 signature over timestamp + raw request body."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        signature = bytes.fromhex(signature_hex)
        public_key.verify(signature, timestamp.encode("utf-8") + raw_body)
        return True
    except (ValueError, InvalidSignature):
        return False


def decode_interaction(raw_body: bytes) -> Dict[str, Any]:
    """Decode a Discord JSON payload after signature validation."""
    payload = json.loads(raw_body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Discord interaction body must be a JSON object")
    return payload


def extract_command_query(
    payload: Dict[str, Any], command_name: str = "ask"
) -> Optional[str]:
    """Extract the required ``question`` string from an /ask interaction."""
    if payload.get("type") != DISCORD_APPLICATION_COMMAND:
        return None
    data = payload.get("data") or {}
    if data.get("name") != command_name:
        return None
    for option in data.get("options") or []:
        if option.get("name") == "question" and isinstance(option.get("value"), str):
            query = option["value"].strip()
            return query or None
    return None


def interaction_to_channel_message(
    payload: Dict[str, Any], query: str
) -> ChannelMessage:
    """Convert a Discord interaction into the transport-neutral envelope."""
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    user_id = str(user.get("id") or "unknown")
    channel_id = str(payload.get("channel_id") or "direct")
    return ChannelMessage(
        id=str(payload.get("id") or "") or None,
        sender=f"discord:{user_id}",
        receiver=f"discord:{channel_id}",
        content=query,
        channel=MessageChannel.DISCORD,
        metadata={
            "guild_id": payload.get("guild_id"),
            "channel_id": payload.get("channel_id"),
            "locale": payload.get("locale"),
            "interaction_id": payload.get("id"),
        },
    )


async def edit_original_response(
    client: httpx.AsyncClient,
    api_base: str,
    application_id: str,
    interaction_token: str,
    content: str,
) -> None:
    """Replace Discord's deferred loading state with the pipeline response."""
    safe_content = content[:DISCORD_MAX_RESPONSE_CHARS]
    url = (
        f"{api_base.rstrip('/')}/webhooks/{application_id}/"
        f"{interaction_token}/messages/@original"
    )
    response = await client.patch(
        url,
        json={
            "content": safe_content,
            "allowed_mentions": {"parse": []},
        },
    )
    response.raise_for_status()
