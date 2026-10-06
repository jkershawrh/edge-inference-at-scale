"""Crash-safe, fail-closed corpus activation for a Lil EVY node."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .corpus_package import validate_corpus_package
from .recovery_authorization import VerifiedRecoveryAuthorization

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_SIGNATURE = re.compile(r"^[A-Za-z0-9+/]{86}==$")
_ZERO_DIGEST = "sha256:" + "0" * 64


class ActivationState(str, Enum):
    STAGED = "STAGED"
    VERIFIED = "VERIFIED"
    INDEXED = "INDEXED"
    CANARY_TESTED = "CANARY_TESTED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    RECOVERY = "RECOVERY"


class ActivationConfigurationError(RuntimeError):
    """Required trust or signing configuration is absent or invalid."""


class ActivationError(RuntimeError):
    def __init__(self, message: str, receipt: "ActivationReceipt") -> None:
        super().__init__(message)
        self.receipt = receipt


class RollbackRejected(ActivationError):
    pass


class ActivationFailed(ActivationError):
    pass


class RecoveryRejected(ActivationError):
    pass


@dataclass(frozen=True)
class ReleaseCandidate:
    digest: str
    sequence: int
    package_path: Path

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("release digest must be sha256:<64 lowercase hex>")
        if self.sequence < 1:
            raise ValueError("release sequence must be positive")


@dataclass(frozen=True)
class VerificationResult:
    """Explicit verifier output; successful return alone never implies trust."""
    signature_verified: bool
    hashes_verified: bool
    scope_verified: bool

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (
            self.signature_verified, self.hashes_verified, self.scope_verified
        )):
            raise TypeError("verification fields must be booleans")

    @property
    def passed(self) -> bool:
        return self.signature_verified and self.hashes_verified and self.scope_verified


@dataclass(frozen=True)
class SmokeTestResult:
    passed: bool
    report_digest: str
    retrieval_p95_ms: float

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.report_digest) or self.retrieval_p95_ms < 0:
            raise ValueError("invalid smoke-test result")


@dataclass(frozen=True)
class RecoveryAuthorization:
    """Release-bound/pre-authorized recovery input.

    This compatibility record is not an exceptional downgrade authorization.
    External exceptional authorization must use the signed-record verifier and
    :meth:`CorpusActivationManager.recover_exceptionally`.
    """
    authorization_id: str
    target_digest: str
    sequence_floor: int
    current_digest: str
    site_id: str

    def __post_init__(self) -> None:
        uuid.UUID(self.authorization_id)
        if not _DIGEST.fullmatch(self.target_digest) or not _DIGEST.fullmatch(self.current_digest):
            raise ValueError("invalid recovery digest")
        if self.sequence_floor < 1:
            raise ValueError("recovery sequence floor must be positive")


ReceiptSigner = Callable[[bytes], Mapping[str, str]]


@dataclass(frozen=True)
class ActivationReceipt:
    receipt_id: str
    node_id: str
    cluster_id: str
    site_id: str
    desired_digest: str
    desired_sequence: int
    activated_digest: Optional[str]
    activated_sequence: Optional[int]
    previous_digest: Optional[str]
    verification: Mapping[str, Any]
    runtime: Mapping[str, str]
    transition_from: str
    transition_to: str
    result: str
    reason_code: str
    smoke_tests: Mapping[str, Any]
    device_counter: int
    time_confidence: str
    receipt_signer: ReceiptSigner = field(repr=False, compare=False)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    signature: Mapping[str, str] = field(init=False)

    def __post_init__(self) -> None:
        canonical = json.dumps(self._unsigned(), sort_keys=True, separators=(",", ":")).encode()
        signature = dict(self.receipt_signer(canonical))
        if (set(signature) != {"key_id", "algorithm", "value"}
                or not _ID.fullmatch(str(signature.get("key_id", "")))
                or signature.get("algorithm") != "Ed25519"
                or not _SIGNATURE.fullmatch(str(signature.get("value", "")))):
            raise ActivationConfigurationError("receipt signer returned an invalid signature")
        object.__setattr__(self, "signature", signature)

    def _unsigned(self) -> Dict[str, Any]:
        return {
            "schema_version": "2.0.0", "record_type": "activation_receipt",
            "receipt_id": self.receipt_id,
            "identity": {"node_id": self.node_id, "cluster_id": self.cluster_id, "site_id": self.site_id},
            "desired": {"digest": self.desired_digest, "sequence": self.desired_sequence},
            "activated": ({"digest": self.activated_digest, "sequence": self.activated_sequence}
                          if self.activated_digest else None),
            "previous_digest": self.previous_digest,
            "verification": dict(self.verification), "runtime": dict(self.runtime),
            "transition": {"from": self.transition_from, "to": self.transition_to},
            "result": self.result, "reason_code": self.reason_code,
            "smoke_tests": dict(self.smoke_tests), "device_counter": self.device_counter,
            "time_confidence": self.time_confidence, "created_at": self.created_at,
        }

    def to_dict(self) -> Dict[str, Any]:
        result = self._unsigned()
        result["signature"] = dict(self.signature)
        return result


class LocalActivationFilesystem:
    def atomic_write_json(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(path))
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def stage_directory(self, source: Path, destination: Path) -> None:
        if not source.is_dir():
            raise FileNotFoundError("corpus package directory does not exist")
        if destination.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / (".%s.%s" % (destination.name, uuid.uuid4().hex))
        try:
            shutil.copytree(str(source), str(temporary))
            os.replace(str(temporary), str(destination))
        finally:
            if temporary.exists():
                shutil.rmtree(str(temporary))


Verifier = Callable[[Path], VerificationResult]
Indexer = Callable[[Path, Path], None]
SmokeTester = Callable[[Path], SmokeTestResult]


class CorpusActivationManager:
    def __init__(self, root: Path, node_id: str, cluster_id: str, site_id: str,
                 filesystem: Optional[LocalActivationFilesystem] = None, *,
                 trusted_public_key_path: Optional[Path] = None,
                 expected_event_id: Optional[str] = None,
                 expected_version: Optional[str] = None,
                 policy_digest: Optional[str] = None,
                 runtime_identity: Optional[Mapping[str, str]] = None,
                 time_confidence: str = "unknown",
                 receipt_signer: Optional[ReceiptSigner] = None) -> None:
        self.root, self.node_id, self.cluster_id, self.site_id = Path(root), node_id, cluster_id, site_id
        self.fs = filesystem or LocalActivationFilesystem()
        self.releases, self.current_path = self.root / "releases", self.root / "current.json"
        self.receipt_counter_path = self.root / "receipt-counter.json"
        self.trusted_public_key_path = Path(trusted_public_key_path) if trusted_public_key_path else None
        self.expected_event_id, self.expected_version = expected_event_id, expected_version
        self.policy_digest, self.runtime_identity = policy_digest, dict(runtime_identity or {})
        self.time_confidence, self.receipt_signer = time_confidence, receipt_signer

    def current(self) -> Dict[str, Any]:
        if not self.current_path.exists():
            return {"active_digest": None, "active_sequence": 0, "sequence_floor": 0,
                    "mode": "uninitialized", "device_counter": 0,
                    "used_recovery_authorizations": []}
        return json.loads(self.current_path.read_text(encoding="utf-8"))

    def release_state(self, digest: str) -> Dict[str, Any]:
        return json.loads((self._release_dir(digest) / "state.json").read_text(encoding="utf-8"))

    def activate(self, candidate: ReleaseCandidate, verifier: Optional[Verifier] = None,
                 indexer: Optional[Indexer] = None,
                 smoke_tester: Optional[SmokeTester] = None) -> ActivationReceipt:
        self._ensure_configured()
        before = self.current()
        active, floor = before["active_digest"], int(before["sequence_floor"])
        if candidate.sequence < floor or (candidate.sequence == floor and candidate.digest != active):
            receipt = self._receipt(candidate, before, active, "rejected", "SEQUENCE_ROLLBACK",
                                    "ACTIVE" if active else "STAGED", "REJECTED",
                                    anti_rollback_verified=False)
            raise RollbackRejected("release sequence does not advance the node floor", receipt)
        if candidate.sequence == floor and candidate.digest == active:
            return self._receipt(candidate, before, active, "success", "ALREADY_ACTIVE",
                                 "ACTIVE", "ACTIVE",
                                 verification=self._verification_dict(VerificationResult(True, True, True), True),
                                 smoke={"passed": True, "report_digest": _ZERO_DIGEST, "retrieval_p95_ms": 0.0})
        return self._prepare_and_commit(candidate, before, "production", None,
                                        verifier, indexer, smoke_tester)

    def recover(self, candidate: ReleaseCandidate, authorization: RecoveryAuthorization,
                verifier: Optional[Verifier] = None, indexer: Optional[Indexer] = None,
                smoke_tester: Optional[SmokeTester] = None) -> ActivationReceipt:
        """Use the existing release-bound/pre-authorized recovery path.

        This method does not accept external exceptional-downgrade authority;
        callers with that signed record must use ``recover_exceptionally``.
        """
        self._ensure_configured()
        before = self.current()
        valid = (authorization.authorization_id not in set(before.get("used_recovery_authorizations", []))
                 and authorization.target_digest == candidate.digest
                 and authorization.sequence_floor == before["sequence_floor"]
                 and authorization.current_digest == before["active_digest"]
                 and authorization.site_id == self.site_id)
        if not valid:
            receipt = self._receipt(candidate, before, before["active_digest"], "rejected",
                                    "RECOVERY_NOT_AUTHORIZED", "ACTIVE", "REJECTED")
            raise RecoveryRejected("recovery authorization does not match node state", receipt)
        return self._prepare_and_commit(candidate, before, "recovery", [authorization.authorization_id],
                                        verifier, indexer, smoke_tester)

    def recover_exceptionally(
        self,
        candidate: ReleaseCandidate,
        authorization: VerifiedRecoveryAuthorization,
        verifier: Optional[Verifier] = None,
        indexer: Optional[Indexer] = None,
        smoke_tester: Optional[SmokeTester] = None,
    ) -> ActivationReceipt:
        """Activate an older, non-pre-authorized release with signed authority."""
        self._ensure_configured()
        if not isinstance(authorization, VerifiedRecoveryAuthorization):
            raise TypeError("exceptional recovery requires a verified signed authorization")
        before = self.current()
        if authorization.blocked_safety_classes:
            receipt = self._receipt(
                candidate,
                before,
                before["active_digest"],
                "rejected",
                "RECOVERY_RESTRICTIONS_UNENFORCEABLE",
                "ACTIVE",
                "REJECTED",
            )
            raise RecoveryRejected(
                "runtime cannot yet enforce signed recovery safety restrictions",
                receipt,
            )
        markers = authorization.replay_markers
        used = set(before.get("used_recovery_authorizations", []))
        valid = (
            candidate.digest == authorization.target_digest
            and candidate.sequence == authorization.target_sequence
            and authorization.current_digest == before["active_digest"]
            and authorization.sequence_floor == before["sequence_floor"]
            and authorization.event_id == self.expected_event_id
            and authorization.site_id == self.site_id
            and not used.intersection(markers)
        )
        if not valid:
            receipt = self._receipt(
                candidate,
                before,
                before["active_digest"],
                "rejected",
                "RECOVERY_NOT_AUTHORIZED",
                "ACTIVE",
                "REJECTED",
            )
            raise RecoveryRejected(
                "signed recovery authorization does not match node state", receipt
            )
        return self._prepare_and_commit(
            candidate,
            before,
            "recovery",
            markers,
            verifier,
            indexer,
            smoke_tester,
        )

    def _prepare_and_commit(self, candidate: ReleaseCandidate, before: Mapping[str, Any],
                            mode: str, authorization_markers: Optional[Sequence[str]],
                            verifier: Optional[Verifier], indexer: Optional[Indexer],
                            smoke_tester: Optional[SmokeTester]) -> ActivationReceipt:
        package_dir, index_dir = self._release_dir(candidate.digest) / "package", self._release_dir(candidate.digest) / "index"
        result = VerificationResult(False, False, False)
        verification = self._verification_dict(result, True)
        smoke = {"passed": False, "report_digest": _ZERO_DIGEST, "retrieval_p95_ms": 0.0}
        last_state = ActivationState.STAGED
        try:
            self.fs.stage_directory(candidate.package_path, package_dir)
            self._record_state(candidate, ActivationState.STAGED)
            result = self._coerce_verification((verifier or self._default_verifier)(package_dir))
            verification = self._verification_dict(result, True)
            if not result.passed:
                raise ValueError("corpus verification checks did not all pass")
            self._record_state(candidate, ActivationState.VERIFIED); last_state = ActivationState.VERIFIED
            index_dir.mkdir(parents=True, exist_ok=True)
            (indexer or self._default_indexer)(package_dir, index_dir)
            self._record_state(candidate, ActivationState.INDEXED); last_state = ActivationState.INDEXED
            tested = (smoke_tester or self._default_smoke_test)(index_dir)
            smoke = {"passed": tested.passed, "report_digest": tested.report_digest,
                     "retrieval_p95_ms": tested.retrieval_p95_ms}
            if not tested.passed:
                raise ValueError("corpus smoke test failed")
            self._record_state(candidate, ActivationState.CANARY_TESTED); last_state = ActivationState.CANARY_TESTED
            self._record_state(candidate, ActivationState.READY); last_state = ActivationState.READY
            used: Sequence[str] = before.get("used_recovery_authorizations", [])
            if authorization_markers:
                used = list(used) + list(authorization_markers)
            next_current = {"active_digest": candidate.digest, "active_sequence": candidate.sequence,
                            "sequence_floor": max(int(before["sequence_floor"]), candidate.sequence),
                            "mode": mode, "device_counter": int(before.get("device_counter", 0)),
                            "used_recovery_authorizations": list(used)}
            success_receipt = self._receipt(candidate, next_current, candidate.digest,
                "recovered" if mode == "recovery" else "success",
                "RECOVERY_ACTIVATED" if mode == "recovery" else "ACTIVATED", "READY",
                "RECOVERY" if mode == "recovery" else "ACTIVE", before.get("active_digest"),
                verification, smoke)
            next_current["device_counter"] = success_receipt.device_counter
            final_state = (
                ActivationState.RECOVERY
                if mode == "recovery"
                else ActivationState.ACTIVE
            )
            # The pointer is the commit record. Persist the release's final
            # state first so a sudden loss of power can leave only an
            # unreferenced candidate, never a pointer to an unfinished state.
            self._record_state(candidate, final_state)
            last_state = final_state
            self.fs.atomic_write_json(self.current_path, next_current)
            return success_receipt
        except ActivationError:
            raise
        except Exception as exc:
            try:
                self._record_state(candidate, ActivationState.REJECTED, str(exc))
            except Exception:
                pass
            after = self.current()
            if after.get("active_digest") == candidate.digest:
                return success_receipt
            receipt = self._receipt(candidate, before, before.get("active_digest"), "failed",
                                    "ACTIVATION_FAILED", last_state.value, "REJECTED",
                                    before.get("active_digest"), verification, smoke)
            raise ActivationFailed("candidate activation failed: %s" % exc, receipt) from exc

    def _ensure_configured(self) -> None:
        if any(not _ID.fullmatch(value) for value in (self.node_id, self.cluster_id, self.site_id)):
            raise ActivationConfigurationError("node, cluster, and site IDs are invalid")
        if not self.trusted_public_key_path or not self.trusted_public_key_path.is_file():
            raise ActivationConfigurationError("a trusted corpus public key is required")
        if not self.expected_event_id or not _ID.fullmatch(self.expected_event_id):
            raise ActivationConfigurationError("an expected corpus event ID is required")
        if not self.expected_version or not _ID.fullmatch(self.expected_version):
            raise ActivationConfigurationError("an expected corpus version is required")
        if not self.policy_digest or not _DIGEST.fullmatch(self.policy_digest):
            raise ActivationConfigurationError("a valid policy digest is required")
        required = {"version", "chunker_digest", "model_digest", "embedding_model_digest"}
        if set(self.runtime_identity) != required or not self.runtime_identity.get("version") or any(
                not _DIGEST.fullmatch(self.runtime_identity[key]) for key in required - {"version"}):
            raise ActivationConfigurationError("complete valid runtime identity is required")
        if self.time_confidence not in {"trusted", "degraded", "unknown"}:
            raise ActivationConfigurationError("time confidence is invalid")
        if not callable(self.receipt_signer):
            raise ActivationConfigurationError("a receipt signer is required")

    def _default_verifier(self, package_dir: Path) -> VerificationResult:
        validate_corpus_package(str(package_dir / "manifest.json"),
            expected_event_id=self.expected_event_id, expected_version=self.expected_version,
            public_key_path=str(self.trusted_public_key_path), require_signature=True)
        return VerificationResult(True, True, True)

    @staticmethod
    def _coerce_verification(value: Any) -> VerificationResult:
        if isinstance(value, VerificationResult):
            return value
        required = {"signature_verified", "hashes_verified", "scope_verified"}
        if not isinstance(value, Mapping) or not required.issubset(value) or any(
                type(value[key]) is not bool for key in required):
            raise TypeError("verifier must return explicit boolean verification fields")
        return VerificationResult(**{key: value[key] for key in required})

    def _verification_dict(self, value: VerificationResult, anti: bool) -> Dict[str, Any]:
        return {"policy_digest": self.policy_digest, "signature_verified": value.signature_verified,
                "hashes_verified": value.hashes_verified, "scope_verified": value.scope_verified,
                "anti_rollback_verified": anti}

    @staticmethod
    def _default_indexer(package_dir: Path, index_dir: Path) -> None:
        source = package_dir / "document_index.json"
        if not source.is_file():
            raise FileNotFoundError("verified package has no document index")
        shutil.copy2(str(source), str(index_dir / "document_index.json"))

    @staticmethod
    def _default_smoke_test(index_dir: Path) -> SmokeTestResult:
        index = index_dir / "document_index.json"
        if not index.is_file():
            raise FileNotFoundError("corpus index was not built")
        return SmokeTestResult(True, "sha256:" + hashlib.sha256(index.read_bytes()).hexdigest(), 0.0)

    def _release_dir(self, digest: str) -> Path:
        if not _DIGEST.fullmatch(digest):
            raise ValueError("invalid release digest")
        return self.releases / digest.split(":", 1)[1]

    def _record_state(self, candidate: ReleaseCandidate, state: ActivationState,
                      error: Optional[str] = None) -> None:
        path = self._release_dir(candidate.digest) / "state.json"
        previous = json.loads(path.read_text()) if path.exists() else {}
        value = {"digest": candidate.digest, "sequence": candidate.sequence, "state": state.value,
                 "history": list(previous.get("history", [])) + [state.value]}
        if error:
            value["reason_code"] = "ACTIVATION_FAILED"
        self.fs.atomic_write_json(path, value)

    def _next_device_counter(self) -> int:
        persisted = 0
        if self.receipt_counter_path.exists():
            value = json.loads(self.receipt_counter_path.read_text(encoding="utf-8"))
            persisted = int(value.get("device_counter", 0))
        active = int(self.current().get("device_counter", 0))
        counter = max(persisted, active) + 1
        self.fs.atomic_write_json(
            self.receipt_counter_path, {"device_counter": counter}
        )
        return counter

    def _receipt(self, candidate: ReleaseCandidate, state: Mapping[str, Any],
                 activated_digest: Optional[str], result: str, reason: str,
                 transition_from: str, transition_to: str,
                 previous_digest: Optional[str] = None,
                 verification: Optional[Mapping[str, Any]] = None,
                 smoke: Optional[Mapping[str, Any]] = None,
                 anti_rollback_verified: bool = True) -> ActivationReceipt:
        return ActivationReceipt(receipt_id=str(uuid.uuid4()), node_id=self.node_id,
            cluster_id=self.cluster_id, site_id=self.site_id, desired_digest=candidate.digest,
            desired_sequence=candidate.sequence, activated_digest=activated_digest,
            activated_sequence=(candidate.sequence if activated_digest == candidate.digest
                                else state.get("active_sequence")),
            previous_digest=previous_digest,
            verification=verification or self._verification_dict(VerificationResult(False, False, False), anti_rollback_verified),
            runtime=self.runtime_identity, transition_from=transition_from, transition_to=transition_to,
            result=result, reason_code=reason,
            smoke_tests=smoke or {"passed": False, "report_digest": _ZERO_DIGEST, "retrieval_p95_ms": 0.0},
            device_counter=self._next_device_counter(),
            time_confidence=self.time_confidence, receipt_signer=self.receipt_signer)
