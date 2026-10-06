"""Signed exceptional-downgrade authorization for disconnected Lil EVY nodes.

This is intentionally not the routine, release-bound recovery path.  It is the
cryptographic policy boundary for serving an older corpus that was not already
authorized by the active release.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_UTC_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SAFETY_CLASSES = {"advisory", "standard", "high", "critical"}
_TOP_FIELDS = {
    "schema_version",
    "record_type",
    "authorization_id",
    "nonce",
    "event_id",
    "site_id",
    "target",
    "current",
    "reason_code",
    "incident_reference",
    "issuer",
    "approvers",
    "validity",
    "policy",
    "signature",
}
_VERIFICATION_SEAL = object()


class RecoveryAuthorizationError(ValueError):
    """An exceptional recovery authorization is invalid or not applicable."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RecoveryAuthorizationError(
                "recovery authorization contains duplicate fields"
            )
        value[key] = item
    return value


def read_recovery_authorization(path: Path) -> Mapping[str, Any]:
    """Read a regular UTF-8 JSON record without accepting duplicate fields."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RecoveryAuthorizationError(
            "recovery authorization is missing or is not a regular file"
        )
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except RecoveryAuthorizationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryAuthorizationError(
            "recovery authorization is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise RecoveryAuthorizationError("recovery authorization must be a JSON object")
    return value


@dataclass(frozen=True)
class RecoveryApprover:
    approver_id: str
    role: str
    independence_group: str


@dataclass(frozen=True)
class VerifiedRecoveryAuthorization:
    """Immutable result produced only after all policy and signature checks."""

    authorization_id: str
    nonce: str
    event_id: str
    site_id: str
    target_digest: str
    target_sequence: int
    current_digest: str
    sequence_floor: int
    reason_code: str
    incident_reference: str
    issuer_id: str
    trust_generation: int
    approvers: Tuple[RecoveryApprover, ...]
    not_before: datetime
    expires_at: datetime
    maximum_offline_seconds: int
    time_basis: str
    blocked_safety_classes: Tuple[str, ...]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _VERIFICATION_SEAL:
            raise RecoveryAuthorizationError(
                "verified authorization objects must come from signature verification"
            )

    @property
    def replay_markers(self) -> Tuple[str, str]:
        return (
            "authorization:" + self.authorization_id,
            "nonce:" + self.nonce,
        )


def _canonical_unsigned(value: Mapping[str, Any]) -> bytes:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    return json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _exact(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RecoveryAuthorizationError(f"{label} fields are invalid")
    return value


def _positive_int(value: Any) -> bool:
    return type(value) is int and value >= 1


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIME.fullmatch(value):
        raise RecoveryAuthorizationError(f"{label} must be canonical UTC time")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise RecoveryAuthorizationError(f"{label} is invalid") from exc


def _trusted_time(value: Optional[datetime], label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RecoveryAuthorizationError(f"{label} requires timezone-aware trusted time")
    return value.astimezone(timezone.utc)


def _public_key(path: Path) -> Ed25519PublicKey:
    if not path.is_file():
        raise RecoveryAuthorizationError("recovery authorization key is missing")
    try:
        key_bytes = path.read_bytes()
        try:
            key = serialization.load_pem_public_key(key_bytes)
        except ValueError:
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(key_bytes.strip(), validate=True)
            )
    except (OSError, ValueError, binascii.Error) as exc:
        raise RecoveryAuthorizationError("recovery authorization key is malformed") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise RecoveryAuthorizationError("recovery authorization key is not Ed25519")
    return key


def verify_recovery_authorization(
    record: Mapping[str, Any],
    *,
    expected_event_id: str,
    expected_site_id: str,
    expected_target_digest: str,
    expected_target_sequence: int,
    current_digest: str,
    sequence_floor: int,
    trusted_key_id: str,
    trusted_public_key_path: Path,
    trust_generation: int,
    used_replay_markers: Sequence[str] = (),
    time_confidence: str,
    now: Optional[datetime] = None,
    last_trusted_time: Optional[datetime] = None,
    monotonic_elapsed_seconds: Optional[float] = None,
) -> VerifiedRecoveryAuthorization:
    """Verify one signed, single-use exceptional recovery authorization.

    Trusted time uses ``now`` directly.  Degraded time is accepted only when
    the signed record opts into anchored-time validation and a non-negative
    monotonic elapsed duration can bound offline use.  Unknown time is rejected.
    """
    value = _exact(record, _TOP_FIELDS, "recovery authorization")
    if value["schema_version"] != "1.0.0" or value["record_type"] != "recovery_authorization":
        raise RecoveryAuthorizationError("recovery authorization type is unsupported")
    try:
        authorization_id = str(uuid.UUID(value["authorization_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RecoveryAuthorizationError("authorization ID is invalid") from exc
    if authorization_id != value["authorization_id"]:
        raise RecoveryAuthorizationError("authorization ID must be canonical UUID")
    nonce = value["nonce"]
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise RecoveryAuthorizationError("authorization nonce is invalid")
    for label in ("event_id", "site_id", "reason_code"):
        if not isinstance(value[label], str) or not _IDENTIFIER.fullmatch(value[label]):
            raise RecoveryAuthorizationError(f"{label} is invalid")
    if value["event_id"] != expected_event_id or value["site_id"] != expected_site_id:
        raise RecoveryAuthorizationError("authorization event or site does not match node")
    if not isinstance(value["incident_reference"], str) or not (
        3 <= len(value["incident_reference"]) <= 256
    ):
        raise RecoveryAuthorizationError("incident reference is invalid")

    target = _exact(value["target"], {"digest", "sequence"}, "target")
    current = _exact(value["current"], {"digest", "sequence_floor"}, "current")
    if not isinstance(target["digest"], str) or not _DIGEST.fullmatch(target["digest"]):
        raise RecoveryAuthorizationError("target digest is invalid")
    if not _positive_int(target["sequence"]):
        raise RecoveryAuthorizationError("target sequence is invalid")
    if not isinstance(current["digest"], str) or not _DIGEST.fullmatch(current["digest"]):
        raise RecoveryAuthorizationError("current digest is invalid")
    if not _positive_int(current["sequence_floor"]):
        raise RecoveryAuthorizationError("sequence floor is invalid")
    if (
        target["digest"] != expected_target_digest
        or target["sequence"] != expected_target_sequence
        or current["digest"] != current_digest
        or current["sequence_floor"] != sequence_floor
    ):
        raise RecoveryAuthorizationError("authorization does not match current node state")
    if target["sequence"] >= current["sequence_floor"]:
        raise RecoveryAuthorizationError("exceptional recovery target must be below the floor")

    issuer = _exact(value["issuer"], {"issuer_id", "trust_generation"}, "issuer")
    if not isinstance(issuer["issuer_id"], str) or not _IDENTIFIER.fullmatch(issuer["issuer_id"]):
        raise RecoveryAuthorizationError("issuer ID is invalid")
    if not _positive_int(issuer["trust_generation"]) or issuer["trust_generation"] != trust_generation:
        raise RecoveryAuthorizationError("authorization trust generation is not current")

    approvers_value = value["approvers"]
    if not isinstance(approvers_value, list) or len(approvers_value) < 2:
        raise RecoveryAuthorizationError("two independent approvers are required")
    approvers = []
    for item in approvers_value:
        approval = _exact(
            item, {"approver_id", "role", "independence_group"}, "approver"
        )
        if any(
            not isinstance(approval[field], str)
            or not _IDENTIFIER.fullmatch(approval[field])
            for field in ("approver_id", "role", "independence_group")
        ):
            raise RecoveryAuthorizationError("approver identity is invalid")
        approvers.append(RecoveryApprover(**approval))
    if len({item.approver_id for item in approvers}) != len(approvers) or len(
        {item.independence_group for item in approvers}
    ) != len(approvers):
        raise RecoveryAuthorizationError("approvers are not independent")

    validity = _exact(
        value["validity"],
        {"not_before", "expires_at", "maximum_offline_seconds", "allow_anchored_time"},
        "validity",
    )
    not_before = _parse_time(validity["not_before"], "not_before")
    expires_at = _parse_time(validity["expires_at"], "expires_at")
    if expires_at <= not_before:
        raise RecoveryAuthorizationError("authorization validity window is empty")
    maximum_offline = validity["maximum_offline_seconds"]
    if not _positive_int(maximum_offline) or type(validity["allow_anchored_time"]) is not bool:
        raise RecoveryAuthorizationError("authorization offline validity is invalid")

    policy = _exact(
        value["policy"], {"serving_allowed", "blocked_safety_classes"}, "policy"
    )
    blocked = policy["blocked_safety_classes"]
    if policy["serving_allowed"] is not True:
        raise RecoveryAuthorizationError("authorization does not permit serving")
    if (
        not isinstance(blocked, list)
        or any(item not in _SAFETY_CLASSES for item in blocked)
        or len(blocked) != len(set(blocked))
    ):
        raise RecoveryAuthorizationError("blocked safety classes are invalid")

    used = set(used_replay_markers)
    markers = ("authorization:" + authorization_id, "nonce:" + nonce)
    if any(marker in used for marker in markers):
        raise RecoveryAuthorizationError("authorization ID or nonce has already been used")

    if time_confidence == "trusted":
        effective_time = _trusted_time(now, "trusted-time validation")
        time_basis = "trusted"
    elif time_confidence == "degraded":
        if validity["allow_anchored_time"] is not True:
            raise RecoveryAuthorizationError("authorization forbids anchored-time validation")
        anchor = _trusted_time(last_trusted_time, "anchored-time validation")
        if (
            isinstance(monotonic_elapsed_seconds, bool)
            or not isinstance(monotonic_elapsed_seconds, (int, float))
            or monotonic_elapsed_seconds < 0
            or monotonic_elapsed_seconds > maximum_offline
        ):
            raise RecoveryAuthorizationError("offline duration cannot be bounded")
        effective_time = anchor + timedelta(seconds=monotonic_elapsed_seconds)
        time_basis = "anchored_monotonic"
    else:
        raise RecoveryAuthorizationError("trusted or bounded degraded time is required")
    if effective_time < not_before or effective_time > expires_at:
        raise RecoveryAuthorizationError("authorization is outside its validity window")

    signature = _exact(value["signature"], {"key_id", "algorithm", "value"}, "signature")
    if signature["key_id"] != trusted_key_id or signature["algorithm"] != "Ed25519":
        raise RecoveryAuthorizationError("authorization signing key is not trusted")
    try:
        signature_bytes = base64.b64decode(signature["value"], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise RecoveryAuthorizationError("authorization signature is malformed") from exc
    if len(signature_bytes) != 64:
        raise RecoveryAuthorizationError("authorization signature is malformed")
    try:
        _public_key(Path(trusted_public_key_path)).verify(
            signature_bytes, _canonical_unsigned(value)
        )
    except RecoveryAuthorizationError:
        raise
    except Exception as exc:
        raise RecoveryAuthorizationError("authorization signature is invalid") from exc

    return VerifiedRecoveryAuthorization(
        authorization_id=authorization_id,
        nonce=nonce,
        event_id=value["event_id"],
        site_id=value["site_id"],
        target_digest=target["digest"],
        target_sequence=target["sequence"],
        current_digest=current["digest"],
        sequence_floor=current["sequence_floor"],
        reason_code=value["reason_code"],
        incident_reference=value["incident_reference"],
        issuer_id=issuer["issuer_id"],
        trust_generation=issuer["trust_generation"],
        approvers=tuple(approvers),
        not_before=not_before,
        expires_at=expires_at,
        maximum_offline_seconds=maximum_offline,
        time_basis=time_basis,
        blocked_safety_classes=tuple(blocked),
        _seal=_VERIFICATION_SEAL,
    )
