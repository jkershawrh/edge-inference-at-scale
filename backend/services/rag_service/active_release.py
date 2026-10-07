"""Fail-closed resolution of the corpus release selected for runtime use.

Activation is deliberately separate from serving.  This module is the narrow
read-side boundary: a RAG process only receives a package after the durable
activation pointer, release state, package identity, hashes, and signature all
agree.
"""
from __future__ import annotations

import json
import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .corpus_package import CorpusValidationError, validate_corpus_package


_DIGEST = re.compile(r"^sha256:([a-f0-9]{64})$")
_POINTER_FIELDS = {
    "active_digest",
    "active_sequence",
    "sequence_floor",
    "mode",
    "device_counter",
    "used_recovery_authorizations",
}
_RECOVERY_POLICY_FIELD = "recovery_serving_policy"
_RECOVERY_POLICY_FIELDS = {
    "authorization_id",
    "authorization_nonce",
    "blocked_safety_classes",
}
_SAFETY_CLASSES = {"advisory", "standard", "high", "critical"}
_AUTHORIZATION_NONCE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_STATE_FIELDS = {"digest", "sequence", "state", "history"}
_ACTIVATION_STATES = {
    "STAGED",
    "VERIFIED",
    "INDEXED",
    "CANARY_TESTED",
    "READY",
    "ACTIVE",
    "REJECTED",
    "RECOVERY",
}


class ActiveReleaseError(RuntimeError):
    """The configured active release is not safe to serve."""


@dataclass(frozen=True)
class RuntimeReleaseStatus:
    """Validated activation state attached to a runtime corpus selection."""

    digest: str
    sequence: int
    sequence_floor: int
    mode: str
    activation_state: str
    device_counter: int
    blocked_safety_classes: Tuple[str, ...]
    recovery_authorization_id: Optional[str]


@dataclass(frozen=True)
class RuntimeCorpusSelection:
    """Immutable, validated information needed to load the active corpus."""

    status: RuntimeReleaseStatus
    package_dir: Path
    manifest_path: Path
    event_id: str
    corpus_version: str
    document_count: int


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ActiveReleaseError("activation metadata contains duplicate fields")
        value[key] = item
    return value


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ActiveReleaseError(f"{label} is missing or is not a regular file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except ActiveReleaseError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActiveReleaseError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ActiveReleaseError(f"{label} must be a JSON object")
    return value


def _positive_integer(value: Any) -> bool:
    return type(value) is int and value >= 1


class ActiveReleaseResolver:
    """Resolve and cryptographically validate the one corpus Lil EVY may serve."""

    def __init__(
        self,
        activation_root: Path,
        *,
        expected_event_id: str,
        expected_version: str,
        trusted_public_key_path: Path,
    ) -> None:
        self.activation_root = Path(activation_root)
        self.expected_event_id = expected_event_id
        self.expected_version = expected_version
        self.trusted_public_key_path = Path(trusted_public_key_path)

    def resolve(self) -> RuntimeCorpusSelection:
        root = self._trusted_root()
        pointer = self._validate_pointer(_read_object(root / "current.json", "active pointer"))
        digest = pointer["active_digest"]
        match = _DIGEST.fullmatch(digest)
        # _validate_pointer has already checked this; retaining the guard keeps
        # path construction locally obvious and safe under future refactors.
        if match is None:  # pragma: no cover - defensive invariant
            raise ActiveReleaseError("active pointer digest is invalid")

        releases_dir = root / "releases"
        if releases_dir.is_symlink() or not releases_dir.is_dir():
            raise ActiveReleaseError("release store is missing or unsafe")
        release_dir = releases_dir / match.group(1)
        if release_dir.is_symlink():
            raise ActiveReleaseError("release directory is missing or escapes activation root")
        package_dir = release_dir / "package"
        manifest_path = package_dir / "manifest.json"
        self._require_contained(root, release_dir, "release directory")
        self._require_contained(root, package_dir, "release package")
        self._require_contained(root, manifest_path, "corpus manifest")

        state = _read_object(release_dir / "state.json", "release state")
        expected_state = "ACTIVE" if pointer["mode"] == "production" else "RECOVERY"
        self._validate_state(state, pointer, expected_state)
        self._validate_package_tree(package_dir)
        try:
            manifest_digest = "sha256:" + hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise ActiveReleaseError("corpus manifest cannot be read") from exc
        if manifest_digest != digest:
            raise ActiveReleaseError("active digest does not match the package manifest")

        if not self.trusted_public_key_path.is_file():
            raise ActiveReleaseError("trusted corpus public key is missing")
        try:
            manifest = validate_corpus_package(
                str(manifest_path),
                expected_event_id=self.expected_event_id,
                expected_version=self.expected_version,
                public_key_path=str(self.trusted_public_key_path),
                require_signature=True,
            )
        except (CorpusValidationError, OSError, ValueError) as exc:
            raise ActiveReleaseError("active corpus package validation failed") from exc

        status = RuntimeReleaseStatus(
            digest=digest,
            sequence=pointer["active_sequence"],
            sequence_floor=pointer["sequence_floor"],
            mode=pointer["mode"],
            activation_state=expected_state,
            device_counter=pointer["device_counter"],
            blocked_safety_classes=tuple(
                pointer.get(_RECOVERY_POLICY_FIELD, {}).get(
                    "blocked_safety_classes", []
                )
            ),
            recovery_authorization_id=pointer.get(
                _RECOVERY_POLICY_FIELD, {}
            ).get("authorization_id"),
        )
        return RuntimeCorpusSelection(
            status=status,
            package_dir=package_dir.resolve(strict=True),
            manifest_path=manifest_path.resolve(strict=True),
            event_id=manifest["event"]["id"],
            corpus_version=manifest["corpus"]["version"],
            document_count=manifest["corpus"]["document_count"],
        )

    def _trusted_root(self) -> Path:
        if self.activation_root.is_symlink() or not self.activation_root.is_dir():
            raise ActiveReleaseError("activation root is missing or unsafe")
        try:
            return self.activation_root.resolve(strict=True)
        except OSError as exc:
            raise ActiveReleaseError("activation root cannot be resolved") from exc

    @staticmethod
    def _validate_pointer(pointer: Mapping[str, Any]) -> Mapping[str, Any]:
        fields = set(pointer)
        if fields not in {
            frozenset(_POINTER_FIELDS),
            frozenset(_POINTER_FIELDS | {_RECOVERY_POLICY_FIELD}),
        }:
            raise ActiveReleaseError("active pointer fields are invalid")
        if not isinstance(pointer["active_digest"], str) or not _DIGEST.fullmatch(
            pointer["active_digest"]
        ):
            raise ActiveReleaseError("active pointer digest is invalid")
        if not _positive_integer(pointer["active_sequence"]):
            raise ActiveReleaseError("active pointer sequence is invalid")
        if not _positive_integer(pointer["sequence_floor"]):
            raise ActiveReleaseError("active pointer sequence floor is invalid")
        if pointer["sequence_floor"] < pointer["active_sequence"]:
            raise ActiveReleaseError("active sequence exceeds the anti-rollback floor")
        if not isinstance(pointer["mode"], str) or pointer["mode"] not in {
            "production",
            "recovery",
        }:
            raise ActiveReleaseError("active pointer mode is invalid")
        if (
            pointer["mode"] == "production"
            and pointer["active_sequence"] != pointer["sequence_floor"]
        ):
            raise ActiveReleaseError("production pointer does not match the sequence floor")
        if type(pointer["device_counter"]) is not int or pointer["device_counter"] < 1:
            raise ActiveReleaseError("active pointer device counter is invalid")
        authorizations = pointer["used_recovery_authorizations"]
        if (
            not isinstance(authorizations, list)
            or any(not isinstance(item, str) or not item for item in authorizations)
            or len(authorizations) != len(set(authorizations))
        ):
            raise ActiveReleaseError("active pointer recovery authorizations are invalid")
        if pointer["mode"] == "recovery" and not authorizations:
            raise ActiveReleaseError("recovery pointer has no recovery authorization")
        policy = pointer.get(_RECOVERY_POLICY_FIELD)
        if policy is not None:
            if pointer["mode"] != "recovery":
                raise ActiveReleaseError(
                    "recovery serving policy is only valid in recovery mode"
                )
            if not isinstance(policy, dict) or set(policy) != _RECOVERY_POLICY_FIELDS:
                raise ActiveReleaseError("recovery serving policy fields are invalid")
            try:
                authorization_id = str(uuid.UUID(policy["authorization_id"]))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ActiveReleaseError(
                    "recovery serving policy authorization ID is invalid"
                ) from exc
            if authorization_id != policy["authorization_id"]:
                raise ActiveReleaseError(
                    "recovery serving policy authorization ID is not canonical"
                )
            nonce = policy["authorization_nonce"]
            if not isinstance(nonce, str) or not _AUTHORIZATION_NONCE.fullmatch(nonce):
                raise ActiveReleaseError(
                    "recovery serving policy authorization nonce is invalid"
                )
            if (
                "authorization:" + authorization_id not in authorizations
                or "nonce:" + nonce not in authorizations
            ):
                raise ActiveReleaseError(
                    "recovery serving policy is not bound to replay state"
                )
            blocked = policy["blocked_safety_classes"]
            if (
                not isinstance(blocked, list)
                or any(
                    not isinstance(item, str) or item not in _SAFETY_CLASSES
                    for item in blocked
                )
                or len(blocked) != len(set(blocked))
            ):
                raise ActiveReleaseError(
                    "recovery serving policy safety classes are invalid"
                )
        return pointer

    @staticmethod
    def _validate_state(
        state: Mapping[str, Any], pointer: Mapping[str, Any], expected_state: str
    ) -> None:
        if set(state) != _STATE_FIELDS:
            raise ActiveReleaseError("release state fields are invalid")
        if state["digest"] != pointer["active_digest"]:
            raise ActiveReleaseError("release state digest does not match active pointer")
        if type(state["sequence"]) is not int or state["sequence"] != pointer["active_sequence"]:
            raise ActiveReleaseError("release state sequence does not match active pointer")
        if state["state"] != expected_state:
            raise ActiveReleaseError("release is not in the pointer's active state")
        history = state["history"]
        if (
            not isinstance(history, list)
            or len(history) < 2
            or any(
                not isinstance(item, str) or item not in _ACTIVATION_STATES
                for item in history
            )
            or history[-2:] != ["READY", expected_state]
        ):
            raise ActiveReleaseError("release state history is inconsistent")

    @staticmethod
    def _require_contained(root: Path, path: Path, label: str) -> None:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ActiveReleaseError(f"{label} is missing or escapes activation root") from exc

    @staticmethod
    def _validate_package_tree(package_dir: Path) -> None:
        if package_dir.is_symlink() or not package_dir.is_dir():
            raise ActiveReleaseError("release package is missing or unsafe")
        try:
            for entry in package_dir.rglob("*"):
                if entry.is_symlink():
                    raise ActiveReleaseError("release package contains a symbolic link")
        except OSError as exc:
            raise ActiveReleaseError("release package cannot be inspected") from exc
