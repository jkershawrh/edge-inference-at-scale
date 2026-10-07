"""Tamper-evident append-only audit ledger for connected Corpus Factory actions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


SCHEMA_VERSION = "1.0.0"
EVENT_TYPES = {
    "acquisition_planned",
    "acquisition_completed",
    "acquisition_failed",
    "source_changed",
    "source_unchanged",
    "release_candidate_built",
    "promotion_evaluated",
    "evaluation_attested",
    "release_signing_authorized",
    "release_signed",
    "release_published",
    "release_revoked",
}
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class AuditLedgerError(ValueError):
    """The ledger is malformed, noncanonical, or has a broken hash chain."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise AuditLedgerError("occurred_at must be an ISO-8601 time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditLedgerError("occurred_at must be an ISO-8601 time") from exc
    if parsed.tzinfo is None:
        raise AuditLedgerError("occurred_at must include a timezone")
    return parsed


def _decode_line(raw: bytes, line_number: int) -> Dict[str, Any]:
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AuditLedgerError("duplicate key on audit line {0}".format(line_number))
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=no_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AuditLedgerError("invalid JSON on audit line {0}".format(line_number)) from exc
    if not isinstance(value, dict):
        raise AuditLedgerError("audit line {0} must be an object".format(line_number))
    if _canonical(value) != raw:
        raise AuditLedgerError("audit line {0} is not canonical JSON".format(line_number))
    return value


def _validate_entry(
    entry: Mapping[str, Any],
    *,
    expected_sequence: int,
    expected_previous: Optional[str],
    previous_time: Optional[datetime],
) -> datetime:
    required = {
        "schema_version",
        "sequence",
        "previous_entry_digest",
        "event_type",
        "occurred_at",
        "actor",
        "subject_id",
        "payload_digest",
        "entry_digest",
    }
    if set(entry) != required:
        raise AuditLedgerError("audit entry fields do not match the contract")
    if entry["schema_version"] != SCHEMA_VERSION:
        raise AuditLedgerError("unsupported audit schema version")
    if entry["sequence"] != expected_sequence:
        raise AuditLedgerError("audit sequence is not contiguous")
    if entry["previous_entry_digest"] != expected_previous:
        raise AuditLedgerError("audit previous-entry digest does not match")
    if entry["event_type"] not in EVENT_TYPES:
        raise AuditLedgerError("unknown audit event type")
    if not isinstance(entry["actor"], str) or not (3 <= len(entry["actor"]) <= 256):
        raise AuditLedgerError("audit actor is invalid")
    if not isinstance(entry["subject_id"], str) or not _ID.fullmatch(entry["subject_id"]):
        raise AuditLedgerError("audit subject_id is invalid")
    if not isinstance(entry["payload_digest"], str) or not _DIGEST.fullmatch(entry["payload_digest"]):
        raise AuditLedgerError("audit payload_digest is invalid")
    occurred_at = _parse_time(entry["occurred_at"])
    if previous_time is not None and occurred_at < previous_time:
        raise AuditLedgerError("audit time moved backwards")
    body = {key: value for key, value in entry.items() if key != "entry_digest"}
    if entry["entry_digest"] != _digest(body):
        raise AuditLedgerError("audit entry digest does not match its body")
    return occurred_at


def _verify_data(data: bytes) -> Dict[str, Any]:
    if data and not data.endswith(b"\n"):
        raise AuditLedgerError("audit ledger has an incomplete final line")
    previous_digest = None
    previous_time = None
    count = 0
    for count, raw in enumerate(data.splitlines(), start=1):
        entry = _decode_line(raw, count)
        previous_time = _validate_entry(
            entry,
            expected_sequence=count,
            expected_previous=previous_digest,
            previous_time=previous_time,
        )
        previous_digest = entry["entry_digest"]
    return {
        "entries": count,
        "last_sequence": count,
        "last_entry_digest": previous_digest,
        "last_occurred_at": previous_time.isoformat() if previous_time is not None else None,
    }


def verify_audit_ledger(path: Path) -> Dict[str, Any]:
    """Verify every canonical JSONL entry and return a compact chain summary."""

    source = Path(path)
    if not source.exists():
        return {"entries": 0, "last_sequence": 0, "last_entry_digest": None, "last_occurred_at": None}
    if source.is_symlink() or not source.is_file():
        raise AuditLedgerError("audit ledger must be a regular file")
    return _verify_data(source.read_bytes())


def append_audit_event(
    path: Path,
    *,
    event_type: str,
    occurred_at: str,
    actor: str,
    subject_id: str,
    payload_digest: str,
) -> Dict[str, Any]:
    """Lock, verify, append, and fsync one chained event."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "r+b", closefd=False) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            summary = _verify_data(handle.read())
            entry_body = {
                "schema_version": SCHEMA_VERSION,
                "sequence": summary["last_sequence"] + 1,
                "previous_entry_digest": summary["last_entry_digest"],
                "event_type": event_type,
                "occurred_at": occurred_at,
                "actor": actor,
                "subject_id": subject_id,
                "payload_digest": payload_digest,
            }
            entry = {**entry_body, "entry_digest": _digest(entry_body)}
            _validate_entry(
                entry,
                expected_sequence=entry["sequence"],
                expected_previous=summary["last_entry_digest"],
                previous_time=(
                    datetime.fromisoformat(summary["last_occurred_at"])
                    if summary["last_occurred_at"] is not None
                    else None
                ),
            )
            handle.seek(0, os.SEEK_END)
            handle.write(_canonical(entry) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
            return entry
    finally:
        os.close(descriptor)
