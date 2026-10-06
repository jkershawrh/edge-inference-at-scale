import json
import uuid
import base64
from pathlib import Path

import pytest

from backend.services.rag_service.activation import (
    ActivationConfigurationError,
    ActivationFailed,
    CorpusActivationManager,
    LocalActivationFilesystem,
    RecoveryAuthorization,
    ReleaseCandidate,
    RollbackRejected,
    SmokeTestResult,
    VerificationResult,
)
from corpus_factory.validator import validate_instance


def _digest(character: str) -> str:
    return "sha256:" + (character * 64)


def _package(tmp_path: Path, name: str) -> Path:
    package = tmp_path / name
    package.mkdir()
    (package / "manifest.json").write_text("{}", encoding="utf-8")
    return package


def _candidate(tmp_path: Path, character: str, sequence: int) -> ReleaseCandidate:
    return ReleaseCandidate(_digest(character), sequence, _package(tmp_path, character))


def _verifier(_package_dir: Path):
    return VerificationResult(True, True, True)


def _indexer(_package_dir: Path, index_dir: Path) -> None:
    (index_dir / "index.bin").write_bytes(b"ready")


def _smoke(index_dir: Path) -> SmokeTestResult:
    assert (index_dir / "index.bin").read_bytes() == b"ready"
    return SmokeTestResult(True, _digest("e"), 12.5)


def _manager(tmp_path: Path, filesystem=None) -> CorpusActivationManager:
    trusted_key = tmp_path / "trusted-public-key.pem"
    trusted_key.write_text("test key boundary", encoding="utf-8")

    def signer(_canonical: bytes):
        return {
            "key_id": "node-001-attestation",
            "algorithm": "Ed25519",
            "value": base64.b64encode(b"\x00" * 64).decode("ascii"),
        }

    return CorpusActivationManager(
        tmp_path / "activation",
        node_id="node-001",
        cluster_id="cluster-001",
        site_id="site-001",
        filesystem=filesystem,
        trusted_public_key_path=trusted_key,
        expected_event_id="flood-2026",
        expected_version="1.0.0",
        policy_digest=_digest("1"),
        runtime_identity={
            "version": "2.0.0",
            "chunker_digest": _digest("2"),
            "model_digest": _digest("3"),
            "embedding_model_digest": _digest("4"),
        },
        time_confidence="trusted",
        receipt_signer=signer,
    )


def _activate(manager: CorpusActivationManager, candidate: ReleaseCandidate):
    return manager.activate(candidate, _verifier, _indexer, _smoke)


def test_successful_activation_tracks_every_gate_and_commits_pointer(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    candidate = _candidate(tmp_path, "a", 1)

    receipt = _activate(manager, candidate)

    assert manager.current() == {
        "active_digest": candidate.digest,
        "active_sequence": 1,
        "sequence_floor": 1,
        "mode": "production",
        "device_counter": 1,
        "used_recovery_authorizations": [],
    }
    assert manager.release_state(candidate.digest)["history"] == [
        "STAGED",
        "VERIFIED",
        "INDEXED",
        "CANARY_TESTED",
        "READY",
        "ACTIVE",
    ]
    assert receipt.result == "success"
    assert receipt.smoke_tests["passed"] is True
    assert receipt.to_dict()["desired"] == {"digest": candidate.digest, "sequence": 1}
    validate_instance(receipt.to_dict(), "activation_receipt")


@pytest.mark.parametrize("failure_name", ["hash", "signature", "verifier"])
def test_verification_failures_preserve_previous_active(
    tmp_path: Path, failure_name: str
) -> None:
    manager = _manager(tmp_path)
    previous = _candidate(tmp_path, "a", 4)
    _activate(manager, previous)
    candidate = _candidate(tmp_path, "b", 5)

    def fail_verification(_path: Path):
        raise ValueError("%s verification failed" % failure_name)

    with pytest.raises(ActivationFailed) as raised:
        manager.activate(candidate, fail_verification, _indexer, _smoke)

    assert manager.current()["active_digest"] == previous.digest
    assert manager.current()["sequence_floor"] == 4
    assert manager.release_state(candidate.digest)["state"] == "REJECTED"
    assert raised.value.receipt.activated_digest == previous.digest
    validate_instance(raised.value.receipt.to_dict(), "activation_receipt")
    # Failure detail is deliberately not copied into the bounded receipt.
    assert "%s verification failed" % failure_name not in json.dumps(
        raised.value.receipt.to_dict()
    ).lower()


@pytest.mark.parametrize(
    "sequence,character",
    [(4, "c"), (5, "d")],
    ids=["lower-sequence", "equal-sequence-different-digest"],
)
def test_lower_and_equivocal_equal_sequences_are_rejected(
    tmp_path: Path, sequence: int, character: str
) -> None:
    manager = _manager(tmp_path)
    active = _candidate(tmp_path, "a", 5)
    _activate(manager, active)

    with pytest.raises(RollbackRejected) as raised:
        _activate(manager, _candidate(tmp_path, character, sequence))

    assert manager.current()["active_digest"] == active.digest
    assert manager.current()["sequence_floor"] == 5
    assert raised.value.receipt.reason_code == "SEQUENCE_ROLLBACK"
    validate_instance(raised.value.receipt.to_dict(), "activation_receipt")


def test_equal_sequence_same_digest_is_idempotent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    candidate = _candidate(tmp_path, "a", 5)
    _activate(manager, candidate)

    receipt = _activate(manager, candidate)

    assert receipt.reason_code == "ALREADY_ACTIVE"
    assert manager.current()["device_counter"] == 1
    validate_instance(receipt.to_dict(), "activation_receipt")


class _PowerFailOnCurrent(LocalActivationFilesystem):
    def __init__(self) -> None:
        self.fail_current = False

    def atomic_write_json(self, path: Path, value) -> None:
        if self.fail_current and path.name == "current.json":
            # Simulate loss of power before the atomic rename.  No partial
            # current file is written and the previous pointer stays valid.
            raise OSError("simulated power loss")
        super().atomic_write_json(path, value)


def test_power_failure_before_pointer_swap_keeps_old_release(tmp_path: Path) -> None:
    filesystem = _PowerFailOnCurrent()
    manager = _manager(tmp_path, filesystem)
    active = _candidate(tmp_path, "a", 2)
    _activate(manager, active)
    candidate = _candidate(tmp_path, "b", 3)
    filesystem.fail_current = True

    with pytest.raises(ActivationFailed):
        _activate(manager, candidate)

    reloaded = _manager(tmp_path).current()
    assert reloaded["active_digest"] == active.digest
    assert reloaded["sequence_floor"] == 2
    assert manager.release_state(candidate.digest)["history"][:5] == [
        "STAGED",
        "VERIFIED",
        "INDEXED",
        "CANARY_TESTED",
        "READY",
    ]


def test_authorized_recovery_serves_old_digest_without_lowering_floor(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    older = _candidate(tmp_path, "a", 6)
    newest = _candidate(tmp_path, "b", 9)
    _activate(manager, older)
    _activate(manager, newest)
    authorization = RecoveryAuthorization(
        authorization_id=str(uuid.uuid4()),
        target_digest=older.digest,
        sequence_floor=9,
        current_digest=newest.digest,
        site_id="site-001",
    )

    receipt = manager.recover(older, authorization, _verifier, _indexer, _smoke)

    current = manager.current()
    assert current["active_digest"] == older.digest
    assert current["active_sequence"] == 6
    assert current["sequence_floor"] == 9
    assert current["mode"] == "recovery"
    assert current["used_recovery_authorizations"] == [authorization.authorization_id]
    assert receipt.result == "recovered"
    assert receipt.to_dict()["activated"]["sequence"] == 6
    assert receipt.previous_digest == newest.digest
    validate_instance(receipt.to_dict(), "activation_receipt")
    receipt_json = json.dumps(receipt.to_dict())
    for forbidden in ("message", "prompt", "answer", "phone", "user_payload"):
        assert forbidden not in receipt_json.lower()


def test_verifier_false_result_cannot_activate(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    candidate = _candidate(tmp_path, "a", 1)

    def failed_signature(_path: Path):
        return VerificationResult(False, True, True)

    with pytest.raises(ActivationFailed) as raised:
        manager.activate(candidate, failed_signature, _indexer, _smoke)

    assert manager.current()["active_digest"] is None
    assert raised.value.receipt.verification["signature_verified"] is False
    validate_instance(raised.value.receipt.to_dict(), "activation_receipt")


def test_rejected_receipts_advance_durable_device_counter(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    active = _candidate(tmp_path, "a", 5)
    _activate(manager, active)

    counters = []
    for character in ("b", "c"):
        with pytest.raises(RollbackRejected) as raised:
            _activate(manager, _candidate(tmp_path, character, 4))
        counters.append(raised.value.receipt.device_counter)
        validate_instance(raised.value.receipt.to_dict(), "activation_receipt")

    assert counters == [2, 3]
    reloaded = _manager(tmp_path)
    with pytest.raises(RollbackRejected) as raised:
        _activate(reloaded, _candidate(tmp_path, "d", 4))
    assert raised.value.receipt.device_counter == 4


@pytest.mark.parametrize("missing", ["signer", "trusted-key", "runtime", "event"])
def test_missing_security_configuration_fails_closed(tmp_path: Path, missing: str) -> None:
    trusted_key = tmp_path / "trusted.pem"
    trusted_key.write_text("key", encoding="utf-8")
    signer = lambda _payload: {
        "key_id": "node-001-attestation",
        "algorithm": "Ed25519",
        "value": base64.b64encode(b"\x00" * 64).decode("ascii"),
    }
    runtime = {
        "version": "2.0.0",
        "chunker_digest": _digest("2"),
        "model_digest": _digest("3"),
        "embedding_model_digest": _digest("4"),
    }
    manager = CorpusActivationManager(
        tmp_path / "activation",
        "node-001",
        "cluster-001",
        "site-001",
        trusted_public_key_path=None if missing == "trusted-key" else trusted_key,
        expected_event_id=None if missing == "event" else "flood-2026",
        expected_version="1.0.0",
        policy_digest=_digest("1"),
        runtime_identity=None if missing == "runtime" else runtime,
        receipt_signer=None if missing == "signer" else signer,
    )
    candidate = _candidate(tmp_path, "a", 1)

    with pytest.raises(ActivationConfigurationError):
        _activate(manager, candidate)

    assert manager.current()["active_digest"] is None
    assert not (tmp_path / "activation" / "releases").exists()
