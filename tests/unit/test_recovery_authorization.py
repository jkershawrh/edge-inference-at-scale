"""Cryptographic policy tests for exceptional Lil EVY recovery."""
import base64
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.services.rag_service.activation import (
    CorpusActivationManager,
    RecoveryRejected,
    ReleaseCandidate,
    SmokeTestResult,
    VerificationResult,
)
from backend.services.rag_service.recovery_authorization import (
    RecoveryAuthorizationError,
    VerifiedRecoveryAuthorization,
    read_recovery_authorization,
    verify_recovery_authorization,
)


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _key(tmp_path: Path):
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "recovery-public-key.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private, public_path


def _record(private: Ed25519PrivateKey):
    value = {
        "schema_version": "1.0.0",
        "record_type": "recovery_authorization",
        "authorization_id": str(uuid.uuid4()),
        "nonce": "QWxwaGEtTm9uY2UtMDAwMDAx",
        "event_id": "flood-2026",
        "site_id": "site-001",
        "target": {"digest": _digest("a"), "sequence": 6},
        "current": {"digest": _digest("b"), "sequence_floor": 9},
        "reason_code": "bad-index",
        "incident_reference": "INC-2026-1042",
        "issuer": {"issuer_id": "recovery-authority", "trust_generation": 7},
        "approvers": [
            {
                "approver_id": "operations-lead",
                "role": "incident-commander",
                "independence_group": "field-operations",
            },
            {
                "approver_id": "safety-lead",
                "role": "safety-reviewer",
                "independence_group": "safety-office",
            },
        ],
        "validity": {
            "not_before": "2026-10-06T11:00:00Z",
            "expires_at": "2026-10-06T14:00:00Z",
            "maximum_offline_seconds": 7200,
            "allow_anchored_time": True,
        },
        "policy": {
            "serving_allowed": True,
            "blocked_safety_classes": ["critical"],
        },
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["signature"] = {
        "key_id": "recovery-key-2026",
        "algorithm": "Ed25519",
        "value": base64.b64encode(private.sign(canonical)).decode(),
    }
    return value


def _verify(record, public_key: Path, **overrides):
    arguments = {
        "expected_event_id": "flood-2026",
        "expected_site_id": "site-001",
        "expected_target_digest": _digest("a"),
        "expected_target_sequence": 6,
        "current_digest": _digest("b"),
        "sequence_floor": 9,
        "trusted_key_id": "recovery-key-2026",
        "trusted_public_key_path": public_key,
        "trust_generation": 7,
        "time_confidence": "trusted",
        "now": NOW,
    }
    arguments.update(overrides)
    return verify_recovery_authorization(record, **arguments)


def test_verifies_signed_single_use_authorization_with_trusted_time(tmp_path: Path) -> None:
    private, public = _key(tmp_path)

    verified = _verify(_record(private), public)

    assert verified.target_digest == _digest("a")
    assert verified.sequence_floor == 9
    assert verified.time_basis == "trusted"
    assert verified.blocked_safety_classes == ("critical",)
    assert verified.replay_markers == (
        "authorization:" + verified.authorization_id,
        "nonce:" + verified.nonce,
    )


def test_accepts_bounded_degraded_time_from_trusted_anchor(tmp_path: Path) -> None:
    private, public = _key(tmp_path)

    verified = _verify(
        _record(private),
        public,
        time_confidence="degraded",
        now=None,
        last_trusted_time=datetime(2026, 10, 6, 11, 30, tzinfo=timezone.utc),
        monotonic_elapsed_seconds=1800,
    )

    assert verified.time_basis == "anchored_monotonic"


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"time_confidence": "unknown", "now": None}, "trusted or bounded"),
        (
            {
                "time_confidence": "degraded",
                "now": None,
                "last_trusted_time": NOW,
                "monotonic_elapsed_seconds": 7201,
            },
            "bounded",
        ),
        ({"now": datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)}, "validity"),
        ({"trust_generation": 6}, "trust generation"),
        ({"expected_site_id": "other-site"}, "event or site"),
        ({"current_digest": _digest("c")}, "current node state"),
        ({"sequence_floor": 8}, "current node state"),
    ],
)
def test_rejects_wrong_scope_state_trust_or_time(
    tmp_path: Path, overrides, match: str
) -> None:
    private, public = _key(tmp_path)

    with pytest.raises(RecoveryAuthorizationError, match=match):
        _verify(_record(private), public, **overrides)


def test_degraded_time_must_be_explicitly_allowed(tmp_path: Path) -> None:
    private, public = _key(tmp_path)
    value = _record(private)
    value["validity"]["allow_anchored_time"] = False
    _resign(value, private)

    with pytest.raises(RecoveryAuthorizationError, match="forbids anchored"):
        _verify(
            value,
            public,
            time_confidence="degraded",
            now=None,
            last_trusted_time=NOW,
            monotonic_elapsed_seconds=1,
        )


def _resign(value, private: Ed25519PrivateKey) -> None:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    value["signature"]["value"] = base64.b64encode(private.sign(canonical)).decode()


def test_rejects_tampering_after_signature(tmp_path: Path) -> None:
    private, public = _key(tmp_path)
    value = _record(private)
    value["target"]["digest"] = _digest("c")

    with pytest.raises(RecoveryAuthorizationError):
        _verify(value, public, expected_target_digest=_digest("c"))


@pytest.mark.parametrize("marker_kind", ["authorization", "nonce"])
def test_rejects_replayed_authorization_id_or_nonce(
    tmp_path: Path, marker_kind: str
) -> None:
    private, public = _key(tmp_path)
    value = _record(private)
    marker_value = (
        value["authorization_id"] if marker_kind == "authorization" else value["nonce"]
    )

    with pytest.raises(RecoveryAuthorizationError, match="already been used"):
        _verify(value, public, used_replay_markers=[marker_kind + ":" + marker_value])


def test_rejects_non_independent_approvers_even_with_valid_signature(tmp_path: Path) -> None:
    private, public = _key(tmp_path)
    value = _record(private)
    value["approvers"][1]["independence_group"] = "field-operations"
    _resign(value, private)

    with pytest.raises(RecoveryAuthorizationError, match="not independent"):
        _verify(value, public)


def test_rejects_unknown_fields_even_with_valid_signature(tmp_path: Path) -> None:
    private, public = _key(tmp_path)
    value = _record(private)
    value["unexpected"] = "ignored-by-loose-parsers"
    _resign(value, private)

    with pytest.raises(RecoveryAuthorizationError, match="fields"):
        _verify(value, public)


def test_reader_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    path = tmp_path / "authorization.json"
    path.write_text('{"schema_version":"1.0.0","schema_version":"1.0.0"}')

    with pytest.raises(RecoveryAuthorizationError, match="duplicate"):
        read_recovery_authorization(path)


def test_verified_value_cannot_be_constructed_without_verifier() -> None:
    with pytest.raises(RecoveryAuthorizationError, match="signature verification"):
        VerifiedRecoveryAuthorization(
            authorization_id=str(uuid.uuid4()),
            nonce="QWxwaGEtTm9uY2UtMDAwMDAx",
            event_id="flood-2026",
            site_id="site-001",
            target_digest=_digest("a"),
            target_sequence=6,
            current_digest=_digest("b"),
            sequence_floor=9,
            reason_code="bad-index",
            incident_reference="INC-1",
            issuer_id="recovery-authority",
            trust_generation=7,
            approvers=(),
            not_before=NOW,
            expires_at=NOW,
            maximum_offline_seconds=1,
            time_basis="trusted",
            blocked_safety_classes=(),
            _seal=None,
        )


def _activated_manager(tmp_path: Path):
    corpus_key = tmp_path / "corpus-key.pem"
    corpus_key.write_text("test boundary", encoding="utf-8")

    def receipt_signer(_payload: bytes):
        return {
            "key_id": "node-001-attestation",
            "algorithm": "Ed25519",
            "value": base64.b64encode(b"\x00" * 64).decode(),
        }

    manager = CorpusActivationManager(
        tmp_path / "activation",
        "node-001",
        "cluster-001",
        "site-001",
        trusted_public_key_path=corpus_key,
        expected_event_id="flood-2026",
        expected_version="1.0.0",
        policy_digest=_digest("c"),
        runtime_identity={
            "version": "2.0.0",
            "chunker_digest": _digest("d"),
            "model_digest": _digest("e"),
            "embedding_model_digest": _digest("f"),
        },
        time_confidence="trusted",
        receipt_signer=receipt_signer,
    )

    def candidate(character: str, sequence: int) -> ReleaseCandidate:
        package = tmp_path / ("package-" + character)
        package.mkdir()
        (package / "manifest.json").write_text("{}", encoding="utf-8")
        return ReleaseCandidate(_digest(character), sequence, package)

    def verifier(_path: Path):
        return VerificationResult(True, True, True)

    def indexer(_package: Path, index: Path) -> None:
        (index / "ready").write_text("ready", encoding="utf-8")

    def smoke(_index: Path):
        return SmokeTestResult(True, _digest("1"), 1.0)

    older, current = candidate("a", 6), candidate("b", 9)
    manager.activate(older, verifier, indexer, smoke)
    manager.activate(current, verifier, indexer, smoke)
    return manager, older, verifier, indexer, smoke


def test_activation_manager_persists_id_and_nonce_and_rejects_replay(
    tmp_path: Path,
) -> None:
    private, public = _key(tmp_path)
    value = _record(private)
    authorization = _verify(value, public)
    manager, older, verifier, indexer, smoke = _activated_manager(tmp_path)

    receipt = manager.recover_exceptionally(
        older, authorization, verifier, indexer, smoke
    )

    assert receipt.result == "recovered"
    assert manager.current()["sequence_floor"] == 9
    assert manager.current()["used_recovery_authorizations"] == list(
        authorization.replay_markers
    )
    assert manager.current()["recovery_serving_policy"] == {
        "authorization_id": authorization.authorization_id,
        "authorization_nonce": authorization.nonce,
        "blocked_safety_classes": ["critical"],
    }
    with pytest.raises(RecoveryRejected):
        manager.recover_exceptionally(older, authorization, verifier, indexer, smoke)


def test_exceptional_recovery_with_empty_restrictions_is_explicitly_persisted(
    tmp_path: Path,
) -> None:
    private, public = _key(tmp_path)
    value = _record(private)
    value["policy"]["blocked_safety_classes"] = []
    _resign(value, private)
    authorization = _verify(value, public)
    manager, older, verifier, indexer, smoke = _activated_manager(tmp_path)

    manager.recover_exceptionally(older, authorization, verifier, indexer, smoke)

    assert manager.current()["recovery_serving_policy"] == {
        "authorization_id": authorization.authorization_id,
        "authorization_nonce": authorization.nonce,
        "blocked_safety_classes": [],
    }
