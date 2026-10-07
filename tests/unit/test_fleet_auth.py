"""Authentication and replay controls for fleet control-plane messages."""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from backend.services.node_manager.fleet_auth import (
    FleetAuthError,
    FleetAuthenticator,
    FleetReplayStore,
    canonical_fleet_message,
)
from backend.services.node_manager import main as node_manager_main
from backend.services.node_manager.fleet_client import FleetMessageSigner, FleetSequenceStore


def _registry(tmp_path):
    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    path = tmp_path / "nodes.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "nodes": {
                    "field-001": {
                        "key_id": "field-001-2026q4",
                        "public_key_pem": public_pem,
                        "enabled": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return private_key, path


def _proof(private_key, *, kind="heartbeat", sequence=1, issued_at=None, payload=None):
    issued_at = issued_at or int(time.time())
    payload = payload or {"node_id": "field-001", "metrics": {}}
    message = canonical_fleet_message(
        kind=kind,
        node_id="field-001",
        key_id="field-001-2026q4",
        issued_at=issued_at,
        sequence=sequence,
        payload=payload,
    )
    return {
        "key_id": "field-001-2026q4",
        "issued_at": issued_at,
        "sequence": sequence,
        "signature": base64.b64encode(private_key.sign(message)).decode("ascii"),
    }


def test_valid_signature_advances_durable_sequence(tmp_path):
    private_key, registry = _registry(tmp_path)
    state = tmp_path / "replay.sqlite3"
    payload = {"node_id": "field-001", "metrics": {"messages_received": 3}}
    proof = _proof(private_key, payload=payload, sequence=7)

    FleetAuthenticator(registry, FleetReplayStore(state)).verify(
        kind="heartbeat", payload=payload, proof=proof
    )

    assert FleetReplayStore(state).last_sequence("field-001") == 7


def test_replay_is_rejected_across_authenticator_restart(tmp_path):
    private_key, registry = _registry(tmp_path)
    state = tmp_path / "replay.sqlite3"
    payload = {"node_id": "field-001", "metrics": {}}
    proof = _proof(private_key, payload=payload, sequence=4)
    FleetAuthenticator(registry, FleetReplayStore(state)).verify(
        kind="heartbeat", payload=payload, proof=proof
    )

    with pytest.raises(FleetAuthError, match="sequence"):
        FleetAuthenticator(registry, FleetReplayStore(state)).verify(
            kind="heartbeat", payload=payload, proof=proof
        )


@pytest.mark.parametrize("mutation", ["payload", "key_id", "signature"])
def test_tampering_or_wrong_key_is_rejected(tmp_path, mutation):
    private_key, registry = _registry(tmp_path)
    payload = {"node_id": "field-001", "metrics": {}}
    proof = _proof(private_key, payload=payload)
    if mutation == "payload":
        payload = {"node_id": "field-001", "metrics": {"messages_received": 999}}
    elif mutation == "key_id":
        proof["key_id"] = "unknown"
    else:
        proof["signature"] = base64.b64encode(b"invalid" * 10).decode("ascii")

    with pytest.raises(FleetAuthError):
        FleetAuthenticator(registry, FleetReplayStore(tmp_path / "state.sqlite3")).verify(
            kind="heartbeat", payload=payload, proof=proof
        )


def test_stale_message_and_disabled_node_fail_closed(tmp_path):
    private_key, registry = _registry(tmp_path)
    payload = {"node_id": "field-001", "metrics": {}}
    proof = _proof(private_key, payload=payload, issued_at=int(time.time()) - 301)
    auth = FleetAuthenticator(
        registry, FleetReplayStore(tmp_path / "state.sqlite3"), max_clock_skew_seconds=300
    )
    with pytest.raises(FleetAuthError, match="timestamp"):
        auth.verify(kind="heartbeat", payload=payload, proof=proof)

    contents = json.loads(registry.read_text(encoding="utf-8"))
    contents["nodes"]["field-001"]["enabled"] = False
    registry.write_text(json.dumps(contents), encoding="utf-8")
    proof = _proof(private_key, payload=payload, sequence=2)
    with pytest.raises(FleetAuthError, match="disabled"):
        FleetAuthenticator(registry, FleetReplayStore(tmp_path / "other.sqlite3")).verify(
            kind="heartbeat", payload=payload, proof=proof
        )


def test_registry_rejects_node_id_key_binding_mismatch(tmp_path):
    private_key, registry = _registry(tmp_path)
    payload = {"node_id": "impostor", "metrics": {}}
    proof = _proof(private_key, payload=payload)
    with pytest.raises(FleetAuthError, match="not enrolled"):
        FleetAuthenticator(registry, FleetReplayStore(tmp_path / "state.sqlite3")).verify(
            kind="heartbeat", payload=payload, proof=proof
        )


@pytest.mark.parametrize("extra_field", ["private_key_pem", "notes", "next_key"])
def test_registry_is_closed_and_cannot_carry_private_or_ambiguous_keys(tmp_path, extra_field):
    _private_key, registry = _registry(tmp_path)
    value = json.loads(registry.read_text())
    value["nodes"]["field-001"][extra_field] = "forbidden"
    registry.write_text(json.dumps(value))
    auth = FleetAuthenticator(registry, FleetReplayStore(tmp_path / "state.sqlite3"))
    with pytest.raises(FleetAuthError, match="fields"):
        auth.validate_registry()


def test_key_rotation_is_exact_and_preserves_node_replay_floor(tmp_path):
    old_key, registry = _registry(tmp_path)
    state = tmp_path / "state.sqlite3"
    payload = {"node_id": "field-001", "metrics": {}}
    FleetAuthenticator(registry, FleetReplayStore(state)).verify(
        kind="heartbeat", payload=payload,
        proof=_proof(old_key, payload=payload, sequence=7),
    )
    new_key = Ed25519PrivateKey.generate()
    value = json.loads(registry.read_text())
    value["nodes"]["field-001"] = {
        "key_id": "field-001-2027q1",
        "public_key_pem": new_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii"),
        "enabled": True,
    }
    registry.write_text(json.dumps(value))

    with pytest.raises(FleetAuthError, match="key id"):
        FleetAuthenticator(registry, FleetReplayStore(state)).verify(
            kind="heartbeat", payload=payload,
            proof=_proof(old_key, payload=payload, sequence=8),
        )
    issued_at = int(time.time())
    message = canonical_fleet_message(
        kind="heartbeat", node_id="field-001", key_id="field-001-2027q1",
        issued_at=issued_at, sequence=8, payload=payload,
    )
    FleetAuthenticator(registry, FleetReplayStore(state)).verify(
        kind="heartbeat", payload=payload,
        proof={
            "key_id": "field-001-2027q1", "issued_at": issued_at,
            "sequence": 8,
            "signature": base64.b64encode(new_key.sign(message)).decode("ascii"),
        },
    )
    assert FleetReplayStore(state).last_sequence("field-001") == 8


def test_replay_database_is_owner_only(tmp_path):
    path = tmp_path / "state.sqlite3"
    FleetReplayStore(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_registry_duplicate_node_or_key_fields_are_rejected(tmp_path):
    _private_key, registry = _registry(tmp_path)
    registry.write_text(
        '{"schema_version":1,"nodes":{"field-001":'
        '{"key_id":"field-001-2026q4","key_id":"shadow-key",'
        '"public_key_pem":"not-used","enabled":true}}}',
        encoding="utf-8",
    )
    with pytest.raises(FleetAuthError, match="duplicate"):
        FleetAuthenticator(
            registry, FleetReplayStore(tmp_path / "state.sqlite3")
        ).validate_registry()


def test_heartbeat_endpoint_requires_proof_by_default(monkeypatch, tmp_path):
    _, registry = _registry(tmp_path)
    monkeypatch.setattr(node_manager_main.settings, "fleet_auth_mode", "required")
    monkeypatch.setattr(node_manager_main.settings, "fleet_node_registry_path", str(registry))
    monkeypatch.setattr(
        node_manager_main.settings, "fleet_replay_state_path", str(tmp_path / "state.sqlite3")
    )
    monkeypatch.setattr(node_manager_main, "_fleet_authenticator", None)
    node_manager_main.manager.nodes.clear()
    node_manager_main.manager.register_node("field-001", "http://field-001:8000", {})

    response = TestClient(node_manager_main.app).post(
        "/nodes/heartbeat", json={"node_id": "field-001", "metrics": {}}
    )

    assert response.status_code == 401
    assert "required" in response.json()["detail"]


def test_heartbeat_endpoint_accepts_signed_bounded_metrics(monkeypatch, tmp_path):
    private_key, registry = _registry(tmp_path)
    monkeypatch.setattr(node_manager_main.settings, "fleet_auth_mode", "required")
    monkeypatch.setattr(node_manager_main.settings, "fleet_node_registry_path", str(registry))
    monkeypatch.setattr(
        node_manager_main.settings, "fleet_replay_state_path", str(tmp_path / "state.sqlite3")
    )
    monkeypatch.setattr(node_manager_main, "_fleet_authenticator", None)
    node_manager_main.manager.nodes.clear()
    node_manager_main.manager.register_node("field-001", "http://field-001:8000", {})
    payload = {"node_id": "field-001", "metrics": {"messages_received": 3}}
    # Defaults are part of the validated, canonical wire contract.
    canonical_payload = {
        "node_id": "field-001",
        "metrics": {
            "messages_received": 3,
            "avg_latency_ms": None,
            "rag_direct": 0,
            "queue_depth": None,
            "load_percent": None,
        },
        "activation": None,
    }
    proof = _proof(private_key, payload=canonical_payload)

    response = TestClient(node_manager_main.app).post(
        "/nodes/heartbeat", json={**payload, "auth": proof}
    )

    assert response.status_code == 200
    assert node_manager_main.manager.nodes["field-001"]["metrics"] == {
        "messages_received": 3,
        "rag_direct": 0,
    }


def test_heartbeat_rejects_unbounded_or_content_bearing_metrics():
    response = TestClient(node_manager_main.app).post(
        "/nodes/heartbeat",
        json={
            "node_id": "field-001",
            "metrics": {"messages_received": 1, "last_question": "private content"},
        },
    )

    assert response.status_code == 422


def test_node_signer_and_controller_interoperate_across_restarts(tmp_path):
    private_key, registry = _registry(tmp_path)
    private_path = tmp_path / "node-key.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    payload = {
        "node_id": "field-001",
        "metrics": {
            "messages_received": 2,
            "avg_latency_ms": None,
            "rag_direct": 1,
            "queue_depth": None,
            "load_percent": None,
        },
        "activation": None,
    }
    sequence_path = tmp_path / "node-sequence.sqlite3"
    proof_one = FleetMessageSigner(
        node_id="field-001",
        key_id="field-001-2026q4",
        private_key_path=private_path,
        sequence_store=FleetSequenceStore(sequence_path),
    ).sign(kind="heartbeat", payload=payload)
    authenticator = FleetAuthenticator(
        registry, FleetReplayStore(tmp_path / "controller-replay.sqlite3")
    )
    authenticator.verify(kind="heartbeat", payload=payload, proof=proof_one)

    proof_two = FleetMessageSigner(
        node_id="field-001",
        key_id="field-001-2026q4",
        private_key_path=private_path,
        sequence_store=FleetSequenceStore(sequence_path),
    ).sign(kind="heartbeat", payload=payload)
    authenticator.verify(kind="heartbeat", payload=payload, proof=proof_two)

    assert proof_two["sequence"] == proof_one["sequence"] + 1
