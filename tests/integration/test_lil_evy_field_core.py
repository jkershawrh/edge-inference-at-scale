"""Field-core integration drill for a disconnected Lil EVY node.

These tests deliberately join existing runtime components at their public
boundaries.  Network services are replaced with deterministic local results,
but routing, fail-closed decisions, signature verification, activation state,
restart loading, and authorized recovery are exercised by production code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.services.message_router.main import MessageRouter
from backend.services.rag_service.activation import (
    CorpusActivationManager,
    RecoveryAuthorization,
    ReleaseCandidate,
)
from backend.shared.config import settings
from backend.shared.models import ChannelMessage, MessageChannel


def _field_router() -> MessageRouter:
    router = MessageRouter()
    router.http_client = None
    router.chat_store = MagicMock()
    router.chat_store.get_history = AsyncMock(return_value=[])
    router.chat_store.add_turn = AsyncMock()
    router.chat_store.get_hunt_state = AsyncMock(return_value=0)
    router.chat_store.set_hunt_state = AsyncMock()
    router.send_response = AsyncMock(return_value=True)
    router.route_to_llm = AsyncMock(return_value="Use the signed local evacuation route.")
    return router


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel",
    [
        MessageChannel.SMS,
        MessageChannel.SIMULATOR,
        MessageChannel.DISCORD,
        MessageChannel.LORA,
    ],
)
async def test_grounded_field_query_uses_same_reasoning_path_for_every_channel(
    monkeypatch: pytest.MonkeyPatch, channel: MessageChannel
) -> None:
    monkeypatch.setattr(settings, "rag_grounding_required", True)
    router = _field_router()
    router.route_to_rag = AsyncMock(
        return_value=(
            "Signed local guidance: use evacuation route blue.",
            0.50,
            "Signed local guidance: use evacuation route blue.",
        )
    )
    message = ChannelMessage(
        sender=f"{channel.value}:resident-1",
        receiver=f"{channel.value}:lil-evy",
        content="Where is the water distribution point?",
        channel=channel,
    )

    response = await router.process_message(message)

    assert response == "Use the signed local evacuation route."
    router.route_to_rag.assert_awaited_once_with(message.content)
    router.route_to_llm.assert_awaited_once()
    assert router.route_to_llm.await_args.args[:2] == (
        message.content,
        "Signed local guidance: use evacuation route blue.",
    )
    if channel == MessageChannel.DISCORD:
        router.send_response.assert_not_awaited()
    else:
        router.send_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_emergency_uses_high_confidence_rag_direct_and_never_calls_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "emergency_rag_enabled", True)
    monkeypatch.setattr(settings, "rag_grounding_required", True)
    router = _field_router()
    guidance = (
        "Flood evacuation: follow blue markers north to Ridge School. "
        "Do not cross moving water; wait for a local responder if blocked."
    )
    router.route_to_rag = AsyncMock(return_value=(guidance, 0.99, guidance))
    message = ChannelMessage(
        sender="lora:resident-7",
        receiver="lora:lil-evy",
        content="Emergency flooding: where do we evacuate?",
        channel=MessageChannel.LORA,
    )

    response = await router.process_message(message)

    assert response == guidance
    router.route_to_llm.assert_not_awaited()
    router.chat_store.add_turn.assert_not_awaited()
    assert router.stats["rag_direct_responses"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "emergency", "expected"),
    [
        (
            "Where is safe drinking water available?",
            False,
            settings.grounding_failure_message,
        ),
        (
            "Medical emergency: where is the field clinic?",
            True,
            settings.emergency_grounding_failure_message,
        ),
    ],
)
async def test_unavailable_rag_fails_closed_without_llm(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    emergency: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(settings, "rag_grounding_required", True)
    monkeypatch.setattr(settings, "emergency_rag_enabled", emergency)
    router = _field_router()
    router.route_to_rag = AsyncMock(return_value=(None, 0.0, None))
    message = ChannelMessage(
        sender="sms:+15550001111",
        receiver="sms:lil-evy",
        content=content,
        channel=MessageChannel.SMS,
    )

    response = await router.process_message(message)

    assert response == expected
    router.route_to_llm.assert_not_awaited()
    if emergency:
        router.chat_store.add_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel",
    [
        MessageChannel.SMS,
        MessageChannel.DISCORD,
        MessageChannel.LORA,
    ],
)
async def test_field_answer_attribution_is_internal_bounded_and_channel_neutral(
    monkeypatch: pytest.MonkeyPatch, channel: MessageChannel
) -> None:
    monkeypatch.setattr(settings, "rag_grounding_required", True)
    active_digest = "sha256:" + "a" * 64
    guidance = "Water is available at Ridge School from 08:00 to 18:00."

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/validate":
            return httpx.Response(200, json={"valid": True})
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "documents": [guidance, "Unselected secondary evidence"],
                    "scores": [0.98, 0.71],
                    "metadata": [
                        {"parent_doc_id": "water-point-ridge"},
                        {"parent_doc_id": "/var/lib/corpus/secret.json"},
                    ],
                    "active_corpus_digest": active_digest,
                    "active_corpus_sequence": 7,
                },
            )
        return httpx.Response(404)

    router = _field_router()
    router.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    message = ChannelMessage(
        sender=f"{channel.value}:resident-private",
        receiver=f"{channel.value}:lil-evy",
        content="Where can I get safe water?",
        channel=channel,
    )
    try:
        response, attribution = await router.process_message_with_attribution(message)
    finally:
        await router.http_client.aclose()

    assert response == guidance
    assert attribution == {
        "schema_version": "1.0",
        "channel": channel.value,
        "retrieval_status": "grounded",
        "response_mode": "rag_direct",
        "grounded": True,
        "active_corpus_digest": active_digest,
        "active_corpus_sequence": 7,
        "evidence": [{"document_id": "water-point-ridge", "score": 0.98}],
    }
    assert router.get_answer_attribution_audit() == [attribution]
    serialized = json.dumps(attribution)
    assert message.content not in serialized
    assert message.sender not in serialized
    assert "/var/lib" not in serialized
    assert guidance not in serialized


@pytest.mark.asyncio
async def test_fail_closed_attribution_does_not_invent_corpus_or_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rag_grounding_required", True)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/validate":
            return httpx.Response(200, json={"valid": True})
        if request.url.path == "/search":
            raise httpx.ConnectError("offline RAG", request=request)
        return httpx.Response(404)

    router = _field_router()
    router.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    message = ChannelMessage(
        sender="sms:private-resident",
        receiver="sms:lil-evy",
        content="Where is safe drinking water available?",
        channel=MessageChannel.SMS,
    )
    try:
        response, attribution = await router.process_message_with_attribution(message)
    finally:
        await router.http_client.aclose()

    assert response == settings.grounding_failure_message
    assert attribution["retrieval_status"] == "unavailable"
    assert attribution["response_mode"] == "refused_grounding"
    assert attribution["grounded"] is False
    assert attribution["active_corpus_digest"] is None
    assert attribution["active_corpus_sequence"] is None
    assert attribution["evidence"] == []
    router.route_to_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_corpus_identity_cannot_be_attached_to_field_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rag_grounding_required", True)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/validate":
            return httpx.Response(200, json={"valid": True})
        if request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "documents": ["Unbound guidance must not be served."],
                    "scores": [0.99],
                    "metadata": [{"parent_doc_id": "unbound-document"}],
                    "active_corpus_digest": "sha256:" + "a" * 64,
                },
            )
        return httpx.Response(404)

    router = _field_router()
    router.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    message = ChannelMessage(
        sender="sms:resident",
        receiver="sms:lil-evy",
        content="Where is the shelter?",
        channel=MessageChannel.SMS,
    )
    try:
        response, attribution = await router.process_message_with_attribution(message)
    finally:
        await router.http_client.aclose()

    assert response == settings.grounding_failure_message
    assert attribution["retrieval_status"] == "unavailable"
    assert attribution["active_corpus_digest"] is None
    assert attribution["evidence"] == []
    router.route_to_llm.assert_not_awaited()


def _write_signed_package(
    root: Path,
    private_key: Ed25519PrivateKey,
    name: str,
    guidance: str,
) -> Path:
    package = root / name
    package.mkdir()
    content_hash = hashlib.sha256(guidance.encode("utf-8")).hexdigest()
    documents = {
        "field-guidance": {
            "id": "field-guidance",
            "text": guidance,
            "category": "emergency",
            "content_hash": content_hash,
            "metadata": {"authority": "county-emergency-office"},
        }
    }
    payloads = {
        "documents.json": documents,
        "categories.json": {"categories": ["emergency"]},
        "document_index.json": {"field-guidance": content_hash},
    }
    files = {}
    for filename, value in payloads.items():
        path = package / filename
        path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        files[filename] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
    manifest = {
        "schema_version": "1.0",
        "event": {"id": "flood-2026", "name": "Flood Response"},
        "corpus": {"version": "field-v1", "document_count": 1},
        "files": files,
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    (package / "manifest.sig").write_text(
        base64.b64encode(private_key.sign(manifest_path.read_bytes())).decode("ascii"),
        encoding="ascii",
    )
    return package


def _release(package: Path, sequence: int) -> ReleaseCandidate:
    digest = "sha256:" + hashlib.sha256(
        (package / "manifest.json").read_bytes()
    ).hexdigest()
    return ReleaseCandidate(digest=digest, sequence=sequence, package_path=package)


def _activation_manager(
    root: Path, public_key_path: Path
) -> CorpusActivationManager:
    def sign_receipt(_payload: bytes):
        return {
            "key_id": "lil-evy-node-attestation",
            "algorithm": "Ed25519",
            "value": base64.b64encode(b"\x00" * 64).decode("ascii"),
        }

    digest = lambda character: "sha256:" + character * 64
    return CorpusActivationManager(
        root,
        node_id="lil-evy-001",
        cluster_id="field-cluster-001",
        site_id="flood-site-001",
        trusted_public_key_path=public_key_path,
        expected_event_id="flood-2026",
        expected_version="field-v1",
        policy_digest=digest("1"),
        runtime_identity={
            "version": "2.0.0",
            "chunker_digest": digest("2"),
            "model_digest": digest("3"),
            "embedding_model_digest": digest("4"),
        },
        time_confidence="trusted",
        receipt_signer=sign_receipt,
    )


def test_signed_active_corpus_survives_restart_and_authorized_recovery(
    tmp_path: Path,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key_path = tmp_path / "trusted-corpus-key.pem"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    older = _release(
        _write_signed_package(
            tmp_path, private_key, "older", "Use Ridge School shelter."
        ),
        1,
    )
    newer = _release(
        _write_signed_package(
            tmp_path, private_key, "newer", "Use North Library shelter."
        ),
        2,
    )
    activation_root = tmp_path / "activation"
    manager = _activation_manager(activation_root, public_key_path)

    manager.activate(older)
    manager.activate(newer)

    restarted = _activation_manager(activation_root, public_key_path)
    assert restarted.current()["active_digest"] == newer.digest
    assert restarted.current()["sequence_floor"] == 2

    authorization = RecoveryAuthorization(
        authorization_id=str(uuid.uuid4()),
        target_digest=older.digest,
        sequence_floor=2,
        current_digest=newer.digest,
        site_id="flood-site-001",
    )
    receipt = restarted.recover(older, authorization)

    recovered_after_restart = _activation_manager(activation_root, public_key_path)
    current = recovered_after_restart.current()
    assert current["active_digest"] == older.digest
    assert current["active_sequence"] == 1
    assert current["sequence_floor"] == 2
    assert current["mode"] == "recovery"
    assert receipt.verification["signature_verified"] is True
    assert receipt.result == "recovered"
