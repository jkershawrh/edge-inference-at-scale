"""Offline trusted-time and revocation state for Lil EVY.

The module deliberately models only signed software evidence.  It does not
claim that the host wall clock, monotonic clock, TPM, GNSS, or NTS is trusted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,255}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ANCHOR_FIELDS = {
    "schema_version", "record_type", "anchor_id", "sequence", "scope",
    "authority", "anchored_at", "max_offline_seconds",
    "previous_anchor_digest", "signature",
}
_SNAPSHOT_FIELDS = {
    "schema_version", "record_type", "snapshot_id", "generation", "scope",
    "authority", "issued_at", "next_update_at", "previous_snapshot_digest",
    "entries", "signature",
}
_SUBJECT_TYPES = {"key", "release", "source", "recovery_authorization"}
_ACTIONS = {"block_activation", "stop_serving", "restrict", "audit_only"}
_SEVERITIES = {"advisory", "standard", "high", "critical"}


class TrustedStateError(ValueError):
    """Signed evidence or persistent state is invalid, replayed, or unsafe."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TrustedStateError("signed evidence contains duplicate fields")
        result[key] = value
    return result


def read_signed_record(path: Path) -> Mapping[str, Any]:
    """Read a bounded trust record without symlinks or duplicate JSON keys."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise TrustedStateError("signed evidence is missing or unsafe")
    try:
        if path.stat().st_size > 1024 * 1024:
            raise TrustedStateError("signed evidence exceeds the size limit")
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates
        )
    except TrustedStateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrustedStateError("signed evidence is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TrustedStateError("signed evidence must be a JSON object")
    return value


@dataclass(frozen=True)
class TimeConfidence:
    state: str
    effective_time: Optional[datetime]
    reason_code: str
    anchor_sequence: int
    anchor_digest: Optional[str]
    offline_age_seconds: Optional[float]


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason_codes: Tuple[str, ...]
    restrictions: Tuple[str, ...]
    time_confidence: str
    effective_time: Optional[str]
    revocation_generation: int


def canonical_unsigned(record: Mapping[str, Any]) -> bytes:
    return json.dumps(
        {key: value for key, value in record.items() if key != "signature"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def signed_record_digest(record: Mapping[str, Any]) -> str:
    payload = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _exact(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise TrustedStateError("%s fields are invalid" % label)
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TrustedStateError("%s is invalid" % label)
    return value


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise TrustedStateError("%s must be a positive integer" % label)
    return value


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise TrustedStateError("%s must be canonical UTC time" % label)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise TrustedStateError("%s is invalid" % label) from exc


def _digest_or_none(value: Any, label: str) -> Optional[str]:
    if value is not None and (not isinstance(value, str) or not _DIGEST.fullmatch(value)):
        raise TrustedStateError("%s is invalid" % label)
    return value


def _verify_signature(
    record: Mapping[str, Any], *, expected_key_id: str, public_key: Ed25519PublicKey
) -> None:
    signature = _exact(record.get("signature"), {"algorithm", "key_id", "value"}, "signature")
    if signature["algorithm"] != "Ed25519" or signature["key_id"] != expected_key_id:
        raise TrustedStateError("signature key role or identity does not match")
    if not isinstance(public_key, Ed25519PublicKey):
        raise TrustedStateError("trusted verification key must be Ed25519")
    try:
        value = base64.b64decode(signature["value"], validate=True)
        if len(value) != 64:
            raise ValueError("length")
        public_key.verify(value, canonical_unsigned(record))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise TrustedStateError("signed evidence verification failed") from exc


def _scope(value: Any, event_id: str, site_id: str, node_id: str) -> None:
    scope = _exact(value, {"event_id", "site_id", "node_id"}, "scope")
    for name in ("event_id", "site_id", "node_id"):
        _identifier(scope[name], "scope " + name)
    if scope != {"event_id": event_id, "site_id": site_id, "node_id": node_id}:
        raise TrustedStateError("signed evidence scope does not match this node")


def _authority(value: Any, expected_key_id: str, trust_generation: int) -> None:
    authority = _exact(value, {"key_id", "trust_generation"}, "authority")
    _identifier(authority["key_id"], "authority key ID")
    _positive(authority["trust_generation"], "trust generation")
    if authority != {"key_id": expected_key_id, "trust_generation": trust_generation}:
        raise TrustedStateError("signed evidence authority is not current")


class OfflineTrustStore:
    """Persist monotonic signed evidence and derive fail-closed eligibility."""

    def __init__(
        self,
        state_path: Path,
        *,
        event_id: str,
        site_id: str,
        node_id: str,
        trust_generation: int,
        boot_id: str,
        monotonic: Callable[[], float],
    ) -> None:
        self.state_path = Path(state_path)
        self.event_id = _identifier(event_id, "event ID")
        self.site_id = _identifier(site_id, "site ID")
        self.node_id = _identifier(node_id, "node ID")
        self.trust_generation = _positive(trust_generation, "trust generation")
        self.boot_id = _identifier(boot_id, "boot ID")
        self.monotonic = monotonic

    def accept_time_anchor(
        self,
        record: Mapping[str, Any],
        *,
        expected_key_id: str,
        public_key: Ed25519PublicKey,
    ) -> TimeConfidence:
        _exact(record, _ANCHOR_FIELDS, "time anchor")
        if record["schema_version"] != "1.0.0" or record["record_type"] != "time_anchor":
            raise TrustedStateError("time anchor type is unsupported")
        _identifier(record["anchor_id"], "anchor ID")
        sequence = _positive(record["sequence"], "anchor sequence")
        _scope(record["scope"], self.event_id, self.site_id, self.node_id)
        _authority(record["authority"], expected_key_id, self.trust_generation)
        anchored_at = _time(record["anchored_at"], "anchor time")
        horizon = _positive(record["max_offline_seconds"], "maximum offline seconds")
        previous = _digest_or_none(record["previous_anchor_digest"], "previous anchor digest")
        _verify_signature(record, expected_key_id=expected_key_id, public_key=public_key)
        state = self._read_state()
        anchor_state = state["time_anchor"]
        if sequence <= anchor_state["sequence"]:
            raise TrustedStateError("time anchor sequence does not advance the replay floor")
        if anchor_state["sequence"] and previous != anchor_state["digest"]:
            raise TrustedStateError("time anchor chain does not match persistent state")
        if not anchor_state["sequence"] and previous is not None:
            raise TrustedStateError("first accepted time anchor cannot claim unknown history")
        prior_confidence = self.time_confidence()
        lower_bound = (
            prior_confidence.effective_time
            if prior_confidence.state == "anchored"
            else (
                _time(anchor_state["anchored_at"], "persisted anchor time")
                if anchor_state["anchored_at"] is not None else None
            )
        )
        if lower_bound is not None and anchored_at < lower_bound:
            raise TrustedStateError("time anchor moves trusted civil time backward")
        tick = self._tick()
        digest = signed_record_digest(record)
        state["time_anchor"] = {
            "sequence": sequence,
            "digest": digest,
            "anchored_at": record["anchored_at"],
            "max_offline_seconds": horizon,
            "accepted_monotonic": tick,
            "boot_id": self.boot_id,
        }
        self._write_state(state)
        return TimeConfidence("anchored", anchored_at, "ANCHOR_ACCEPTED", sequence, digest, 0.0)

    def has_current_time_anchor(self, record: Mapping[str, Any]) -> bool:
        """Return true only when this exact signed record is already installed."""
        state = self._read_state()["time_anchor"]
        return (
            type(record.get("sequence")) is int
            and state["sequence"] == record["sequence"]
            and state["digest"] == signed_record_digest(record)
        )

    def time_confidence(self) -> TimeConfidence:
        state = self._read_state()["time_anchor"]
        if state["sequence"] == 0:
            return TimeConfidence("untrusted", None, "MISSING_TIME_ANCHOR", 0, None, None)
        if state["boot_id"] != self.boot_id:
            return TimeConfidence(
                "untrusted", None, "MONOTONIC_EPOCH_CHANGED", state["sequence"],
                state["digest"], None,
            )
        current = self._tick()
        if current < state["accepted_monotonic"]:
            return TimeConfidence(
                "untrusted", None, "MONOTONIC_CLOCK_MOVED_BACKWARD",
                state["sequence"], state["digest"], None,
            )
        elapsed = current - state["accepted_monotonic"]
        if elapsed > state["max_offline_seconds"]:
            return TimeConfidence(
                "untrusted", None, "TIME_ANCHOR_EXPIRED", state["sequence"],
                state["digest"], elapsed,
            )
        effective = _time(state["anchored_at"], "persisted anchor time") + timedelta(seconds=elapsed)
        return TimeConfidence(
            "anchored", effective, "ANCHOR_WITHIN_OFFLINE_HORIZON",
            state["sequence"], state["digest"], elapsed,
        )

    def accept_revocation_snapshot(
        self,
        record: Mapping[str, Any],
        *,
        expected_key_id: str,
        public_key: Ed25519PublicKey,
    ) -> str:
        _exact(record, _SNAPSHOT_FIELDS, "revocation snapshot")
        if record["schema_version"] != "1.0.0" \
                or record["record_type"] != "revocation_snapshot":
            raise TrustedStateError("revocation snapshot type is unsupported")
        _identifier(record["snapshot_id"], "snapshot ID")
        generation = _positive(record["generation"], "revocation generation")
        _scope(record["scope"], self.event_id, self.site_id, self.node_id)
        _authority(record["authority"], expected_key_id, self.trust_generation)
        issued = _time(record["issued_at"], "snapshot issuance time")
        next_update = _time(record["next_update_at"], "snapshot next-update time")
        if next_update <= issued:
            raise TrustedStateError("snapshot next-update time must follow issuance")
        previous = _digest_or_none(record["previous_snapshot_digest"], "previous snapshot digest")
        entries = self._validate_entries(record["entries"])
        _verify_signature(record, expected_key_id=expected_key_id, public_key=public_key)
        state = self._read_state()
        snapshot = state["revocation_snapshot"]
        if generation <= snapshot["generation"]:
            raise TrustedStateError("revocation generation does not advance the replay floor")
        if snapshot["generation"] and previous != snapshot["digest"]:
            raise TrustedStateError("revocation snapshot chain does not match persistent state")
        if not snapshot["generation"] and previous is not None:
            raise TrustedStateError("first accepted revocation snapshot cannot claim unknown history")
        confidence = self.time_confidence()
        if confidence.state == "anchored" and issued > confidence.effective_time:
            raise TrustedStateError("revocation snapshot is future-issued")
        digest = signed_record_digest(record)
        state["revocation_snapshot"] = {
            "generation": generation,
            "digest": digest,
            "issued_at": record["issued_at"],
            "next_update_at": record["next_update_at"],
            "entries": entries,
        }
        self._write_state(state)
        return digest

    def has_current_revocation_snapshot(self, record: Mapping[str, Any]) -> bool:
        """Return true only when this exact signed snapshot is already installed."""
        state = self._read_state()["revocation_snapshot"]
        return (
            type(record.get("generation")) is int
            and state["generation"] == record["generation"]
            and state["digest"] == signed_record_digest(record)
        )

    def assess_eligibility(
        self,
        *,
        purpose: str,
        subjects: Mapping[str, Sequence[str]],
        max_snapshot_age_seconds: int,
        require_fresh_time: bool = True,
    ) -> EligibilityDecision:
        if purpose not in {"activation", "serving"}:
            raise TrustedStateError("eligibility purpose is invalid")
        max_age = _positive(max_snapshot_age_seconds, "maximum snapshot age")
        for subject_type, identifiers in subjects.items():
            if subject_type not in _SUBJECT_TYPES or not isinstance(identifiers, Sequence) \
                    or isinstance(identifiers, (str, bytes)):
                raise TrustedStateError("eligibility subjects are invalid")
            for identifier in identifiers:
                if not isinstance(identifier, str) or not identifier:
                    raise TrustedStateError("eligibility subject identity is invalid")
        confidence = self.time_confidence()
        state = self._read_state()["revocation_snapshot"]
        reasons = []
        restrictions = []
        if require_fresh_time and confidence.state == "untrusted":
            reasons.append("TIME_UNTRUSTED")
        if state["generation"] == 0:
            reasons.append("REVOCATION_SNAPSHOT_MISSING")
        elif confidence.state == "untrusted":
            reasons.append("REVOCATION_FRESHNESS_UNVERIFIABLE")
        else:
            issued = _time(state["issued_at"], "persisted snapshot issuance time")
            next_update = _time(state["next_update_at"], "persisted snapshot next-update time")
            now = confidence.effective_time
            if now >= next_update or (now - issued).total_seconds() > max_age:
                reasons.append("REVOCATION_SNAPSHOT_STALE")
            for entry in state["entries"]:
                if entry["subject_id"] not in set(subjects.get(entry["subject_type"], ())):
                    continue
                if _time(entry["effective_at"], "revocation effective time") > now:
                    continue
                action = entry["action"]
                if action == "audit_only":
                    continue
                if action == "restrict":
                    restrictions.append(entry["subject_type"] + ":" + entry["subject_id"])
                elif action == "block_activation" and purpose == "activation":
                    reasons.append("SUBJECT_BLOCKS_ACTIVATION")
                elif action == "stop_serving" and purpose == "serving":
                    reasons.append("SUBJECT_STOPPED_SERVING")
        eligible = not reasons and not restrictions
        return EligibilityDecision(
            eligible=eligible,
            reason_codes=tuple(sorted(set(reasons))),
            restrictions=tuple(sorted(set(restrictions))),
            time_confidence=confidence.state,
            effective_time=(
                confidence.effective_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                if confidence.effective_time else None
            ),
            revocation_generation=state["generation"],
        )

    def health_payload(self, *, max_snapshot_age_seconds: int) -> Mapping[str, Any]:
        decision = self.assess_eligibility(
            purpose="serving", subjects={},
            max_snapshot_age_seconds=max_snapshot_age_seconds,
        )
        return {
            "time_confidence": decision.time_confidence,
            "effective_time": decision.effective_time,
            "revocation_generation": decision.revocation_generation,
            "offline_trust_eligible": decision.eligible,
            "offline_trust_reasons": list(decision.reason_codes),
        }

    @staticmethod
    def _validate_entries(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, list):
            raise TrustedStateError("revocation entries must be a list")
        result = []
        seen = set()
        fields = {"subject_type", "subject_id", "reason_code", "effective_at", "severity", "action"}
        for item in value:
            entry = _exact(item, fields, "revocation entry")
            if entry["subject_type"] not in _SUBJECT_TYPES:
                raise TrustedStateError("revocation subject type is invalid")
            if not isinstance(entry["subject_id"], str) or not entry["subject_id"]:
                raise TrustedStateError("revocation subject identity is invalid")
            _identifier(entry["reason_code"], "revocation reason code")
            _time(entry["effective_at"], "revocation effective time")
            if entry["severity"] not in _SEVERITIES or entry["action"] not in _ACTIONS:
                raise TrustedStateError("revocation severity or action is invalid")
            marker = (entry["subject_type"], entry["subject_id"], entry["action"])
            if marker in seen:
                raise TrustedStateError("duplicate revocation entry")
            seen.add(marker)
            result.append(dict(entry))
        return result

    def _tick(self) -> float:
        try:
            value = self.monotonic()
        except Exception as exc:
            raise TrustedStateError("monotonic clock is unavailable") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise TrustedStateError("monotonic clock value is invalid")
        return float(value)

    def _read_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema_version": "1.0.0",
                "event_id": self.event_id,
                "site_id": self.site_id,
                "node_id": self.node_id,
                "trust_generation": self.trust_generation,
                "time_anchor": {
                    "sequence": 0, "digest": None, "anchored_at": None,
                    "max_offline_seconds": 0, "accepted_monotonic": None,
                    "boot_id": None,
                },
                "revocation_snapshot": {
                    "generation": 0, "digest": None, "issued_at": None,
                    "next_update_at": None, "entries": [],
                },
            }
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise TrustedStateError("offline trust state is unsafe")
        try:
            value = json.loads(
                self.state_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicates,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TrustedStateError("offline trust state is invalid") from exc
        expected = {
            "schema_version", "event_id", "site_id", "node_id",
            "trust_generation", "time_anchor", "revocation_snapshot",
        }
        if not isinstance(value, dict) or set(value) != expected \
                or value["schema_version"] != "1.0.0" \
                or (value["event_id"], value["site_id"], value["node_id"], value["trust_generation"]) \
                != (self.event_id, self.site_id, self.node_id, self.trust_generation):
            raise TrustedStateError("offline trust state identity is invalid")
        _exact(value["time_anchor"], {
            "sequence", "digest", "anchored_at", "max_offline_seconds",
            "accepted_monotonic", "boot_id",
        }, "persisted time anchor")
        _exact(value["revocation_snapshot"], {
            "generation", "digest", "issued_at", "next_update_at", "entries",
        }, "persisted revocation snapshot")
        anchor = value["time_anchor"]
        snapshot = value["revocation_snapshot"]
        if type(anchor["sequence"]) is not int or anchor["sequence"] < 0 \
                or type(anchor["max_offline_seconds"]) is not int \
                or anchor["max_offline_seconds"] < 0:
            raise TrustedStateError("persisted time anchor counters are invalid")
        if anchor["sequence"] == 0:
            if anchor != {
                "sequence": 0, "digest": None, "anchored_at": None,
                "max_offline_seconds": 0, "accepted_monotonic": None,
                "boot_id": None,
            }:
                raise TrustedStateError("empty persisted time anchor is inconsistent")
        else:
            if _digest_or_none(anchor["digest"], "persisted anchor digest") is None:
                raise TrustedStateError("persisted anchor digest is missing")
            _time(anchor["anchored_at"], "persisted anchor time")
            _identifier(anchor["boot_id"], "persisted boot ID")
            if isinstance(anchor["accepted_monotonic"], bool) or not isinstance(
                anchor["accepted_monotonic"], (int, float)
            ) or anchor["accepted_monotonic"] < 0 or anchor["max_offline_seconds"] < 1:
                raise TrustedStateError("persisted time anchor timing is invalid")
        if type(snapshot["generation"]) is not int or snapshot["generation"] < 0:
            raise TrustedStateError("persisted revocation generation is invalid")
        if snapshot["generation"] == 0:
            if snapshot != {
                "generation": 0, "digest": None, "issued_at": None,
                "next_update_at": None, "entries": [],
            }:
                raise TrustedStateError("empty persisted revocation snapshot is inconsistent")
        else:
            if _digest_or_none(snapshot["digest"], "persisted snapshot digest") is None:
                raise TrustedStateError("persisted snapshot digest is missing")
            issued = _time(snapshot["issued_at"], "persisted snapshot issuance time")
            if _time(snapshot["next_update_at"], "persisted snapshot next-update time") <= issued:
                raise TrustedStateError("persisted snapshot validity is invalid")
            self._validate_entries(snapshot["entries"])
        return value

    def _write_state(self, value: Mapping[str, Any]) -> None:
        if self.state_path.parent.is_symlink():
            raise TrustedStateError("offline trust state directory is unsafe")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".%s." % self.state_path.name, dir=str(self.state_path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(str(temporary), str(self.state_path))
            directory = os.open(str(self.state_path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()
