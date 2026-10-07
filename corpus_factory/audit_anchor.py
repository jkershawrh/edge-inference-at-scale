"""Externally witnessed checkpoints for the Corpus Factory audit ledger.

The application prepares deterministic bytes and verifies detached receipts. It
never loads or receives an external witness private key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from corpus_factory.audit import AuditLedgerError, verify_audit_ledger
from corpus_factory.validator import ContractValidationError, validate_instance


SCHEMA_VERSION = "1.0.0"


class AuditAnchorError(ValueError):
    """Raised when a checkpoint or external witness chain fails closed."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AuditAnchorError(f"value is not canonical JSON: {exc}") from exc


def object_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AuditAnchorError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AuditAnchorError(f"{label} must include a timezone")
    return parsed


def load_ed25519_public_key(value: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(value)
    except (TypeError, ValueError) as exc:
        raise AuditAnchorError("witness public key is not valid PEM") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AuditAnchorError("witness public key must be Ed25519")
    return key


def public_key_id(key: Ed25519PublicKey) -> str:
    der = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def _ledger_prefix(path: Path, entry_sequence: int) -> Dict[str, Any]:
    try:
        summary = verify_audit_ledger(path)
        data = path.read_bytes()
    except (OSError, AuditLedgerError) as exc:
        raise AuditAnchorError(f"audit ledger verification failed: {exc}") from exc
    if entry_sequence < 1 or entry_sequence > summary["last_sequence"]:
        raise AuditAnchorError("checkpoint entry sequence is outside the audit ledger")
    lines = data.splitlines(keepends=True)
    prefix = b"".join(lines[:entry_sequence])
    try:
        entry = json.loads(lines[entry_sequence - 1])
    except (json.JSONDecodeError, IndexError) as exc:  # ledger verifier should prevent this
        raise AuditAnchorError("cannot resolve checkpoint audit entry") from exc
    return {
        "entry_sequence": entry_sequence,
        "entry_digest": entry["entry_digest"],
        "byte_size": len(prefix),
        "prefix_digest": bytes_digest(prefix),
        "last_occurred_at": entry["occurred_at"],
        "ledger_entries": summary["last_sequence"],
    }


def _validate_checkpoint_against_ledger(
    checkpoint: Mapping[str, Any], ledger_path: Path
) -> None:
    try:
        validate_instance(checkpoint, "corpus_audit_checkpoint")
    except ContractValidationError as exc:
        raise AuditAnchorError(f"audit checkpoint contract failed: {exc}") from exc
    observed = _ledger_prefix(ledger_path, checkpoint["ledger"]["entry_sequence"])
    for field in ("entry_sequence", "entry_digest", "byte_size", "prefix_digest"):
        if checkpoint["ledger"][field] != observed[field]:
            raise AuditAnchorError(f"audit checkpoint {field} does not match ledger prefix")


def prepare_checkpoint(
    ledger_path: Path | str,
    *,
    ledger_id: str,
    requester_identity: str,
    created_at: str,
    previous_receipt: Mapping[str, Any] | None = None,
    previous_public_key: Ed25519PublicKey | None = None,
) -> Dict[str, Any]:
    """Checkpoint the current complete ledger head after validating predecessor."""

    ledger_path = Path(ledger_path)
    try:
        summary = verify_audit_ledger(ledger_path)
    except (OSError, AuditLedgerError) as exc:
        raise AuditAnchorError(f"audit ledger verification failed: {exc}") from exc
    if summary["last_sequence"] < 1:
        raise AuditAnchorError("an empty audit ledger cannot be externally anchored")
    created_time = _parse_time(created_at, "created_at")
    if created_time < _parse_time(summary["last_occurred_at"], "last_occurred_at"):
        raise AuditAnchorError("checkpoint cannot predate the audit ledger head")

    if previous_receipt is None:
        checkpoint_sequence = 1
        previous_anchor = {"receipt_id": None, "receipt_digest": None}
    else:
        if previous_public_key is None:
            raise AuditAnchorError("previous receipt requires its trusted public key")
        verify_receipt(
            ledger_path,
            previous_receipt,
            previous_public_key,
            as_of=created_at,
        )
        previous_checkpoint = previous_receipt["checkpoint"]
        if previous_checkpoint["ledger"]["ledger_id"] != ledger_id:
            raise AuditAnchorError("previous receipt names a different ledger")
        if summary["last_sequence"] <= previous_checkpoint["ledger"]["entry_sequence"]:
            raise AuditAnchorError("new checkpoint must advance the anchored ledger head")
        checkpoint_sequence = previous_checkpoint["checkpoint_sequence"] + 1
        previous_anchor = {
            "receipt_id": previous_receipt["receipt_id"],
            "receipt_digest": object_digest(previous_receipt),
        }

    prefix = _ledger_prefix(ledger_path, summary["last_sequence"])
    body: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "corpus_audit_checkpoint",
        "checkpoint_sequence": checkpoint_sequence,
        "ledger": {
            "ledger_id": ledger_id,
            "entry_sequence": prefix["entry_sequence"],
            "entry_digest": prefix["entry_digest"],
            "byte_size": prefix["byte_size"],
            "prefix_digest": prefix["prefix_digest"],
        },
        "previous_anchor": previous_anchor,
        "requester_identity": requester_identity,
        "created_at": created_at,
    }
    checkpoint = {"checkpoint_id": object_digest(body), **body}
    try:
        validate_instance(checkpoint, "corpus_audit_checkpoint")
    except ContractValidationError as exc:
        raise AuditAnchorError(f"audit checkpoint contract failed: {exc}") from exc
    return checkpoint


def prepare_witness_statement(
    checkpoint: Mapping[str, Any],
    *,
    witness_identity: str,
    witness_organization: str,
    signed_at: str,
    public_key: Ed25519PublicKey,
    key_valid_from: str,
    key_expires_at: str,
    revocation_generation: int,
    revocation_checked_at: str,
) -> Dict[str, Any]:
    """Prepare exact bytes for the external witness; no private key is accepted."""

    try:
        validate_instance(checkpoint, "corpus_audit_checkpoint")
    except ContractValidationError as exc:
        raise AuditAnchorError(f"audit checkpoint contract failed: {exc}") from exc
    signed_time = _parse_time(signed_at, "signed_at")
    created_time = _parse_time(checkpoint["created_at"], "created_at")
    key_start = _parse_time(key_valid_from, "key_valid_from")
    key_end = _parse_time(key_expires_at, "key_expires_at")
    checked_time = _parse_time(revocation_checked_at, "revocation_checked_at")
    if witness_identity == checkpoint["requester_identity"]:
        raise AuditAnchorError("external witness must be independent of requester")
    if signed_time < created_time:
        raise AuditAnchorError("witness signature cannot predate checkpoint")
    if not key_start <= signed_time < key_end:
        raise AuditAnchorError("witness signing time must be inside key validity")
    if checked_time > signed_time:
        raise AuditAnchorError("revocation snapshot cannot postdate signing")
    if isinstance(revocation_generation, bool) or revocation_generation < 1:
        raise AuditAnchorError("revocation generation must be a positive integer")

    body: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "corpus_audit_anchor_receipt",
        "checkpoint": dict(checkpoint),
        "witness": {
            "identity": witness_identity,
            "organization": witness_organization,
            "role": "external-audit-witness",
            "signed_at": signed_at,
        },
        "signing_key": {
            "key_id": public_key_id(public_key),
            "role": "audit-witness",
            "algorithm": "Ed25519",
            "valid_from": key_valid_from,
            "expires_at": key_expires_at,
        },
        "revocation_snapshot": {
            "generation": revocation_generation,
            "checked_at": revocation_checked_at,
            "key_revoked_at": None,
            "key_revocation_reason": None,
        },
    }
    return {"receipt_id": object_digest(body), **body}


def signing_payload(statement: Mapping[str, Any]) -> bytes:
    if "signature" in statement:
        raise AuditAnchorError("witness statement cannot already contain a signature")
    return canonical_json(statement).encode("utf-8")


def finalize_receipt(
    statement: Mapping[str, Any], signature: bytes, public_key: Ed25519PublicKey
) -> Dict[str, Any]:
    if len(signature) != 64:
        raise AuditAnchorError("Ed25519 signature must be 64 bytes")
    if statement.get("signing_key", {}).get("key_id") != public_key_id(public_key):
        raise AuditAnchorError("witness statement key ID does not match public key")
    try:
        public_key.verify(signature, signing_payload(statement))
    except InvalidSignature as exc:
        raise AuditAnchorError("external witness signature is invalid") from exc
    receipt = {
        **statement,
        "signature": {"value": base64.b64encode(signature).decode("ascii")},
    }
    try:
        validate_instance(receipt, "corpus_audit_anchor_receipt")
    except ContractValidationError as exc:
        raise AuditAnchorError(f"audit anchor receipt contract failed: {exc}") from exc
    return receipt


def verify_receipt(
    ledger_path: Path | str,
    receipt: Mapping[str, Any],
    public_key: Ed25519PublicKey,
    *,
    as_of: str,
    revoked_key_ids: Sequence[str] = (),
) -> None:
    try:
        validate_instance(receipt, "corpus_audit_anchor_receipt")
    except ContractValidationError as exc:
        raise AuditAnchorError(f"audit anchor receipt contract failed: {exc}") from exc
    now = _parse_time(as_of, "as_of")
    signed_at = _parse_time(receipt["witness"]["signed_at"], "signed_at")
    if now < signed_at:
        raise AuditAnchorError("trusted verification time predates witness signature")
    key_id = public_key_id(public_key)
    if receipt["signing_key"]["key_id"] != key_id:
        raise AuditAnchorError("witness key ID does not match trusted public key")
    if receipt["revocation_snapshot"]["key_revoked_at"] is not None:
        raise AuditAnchorError("witness key was revoked in the signed snapshot")
    if key_id in set(revoked_key_ids):
        raise AuditAnchorError("witness key appears in external revocation state")
    _validate_checkpoint_against_ledger(receipt["checkpoint"], Path(ledger_path))
    statement = {key: value for key, value in receipt.items() if key != "signature"}
    try:
        signature = base64.b64decode(receipt["signature"]["value"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AuditAnchorError("external witness signature is malformed") from exc
    try:
        public_key.verify(signature, signing_payload(statement))
    except InvalidSignature as exc:
        raise AuditAnchorError("external witness signature is invalid") from exc


def verify_anchor_chain(
    ledger_path: Path | str,
    receipts: Sequence[Mapping[str, Any]],
    trusted_public_keys: Mapping[str, Ed25519PublicKey],
    *,
    as_of: str,
    revoked_key_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Offline-verify receipts, monotonicity, predecessor links, and ledger prefixes."""

    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence) or not receipts:
        raise AuditAnchorError("at least one audit anchor receipt is required")
    by_sequence: Dict[int, Mapping[str, Any]] = {}
    receipt_ids: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise AuditAnchorError("each audit anchor receipt must be an object")
        try:
            validate_instance(receipt, "corpus_audit_anchor_receipt")
        except ContractValidationError as exc:
            raise AuditAnchorError(f"audit anchor receipt contract failed: {exc}") from exc
        sequence = receipt["checkpoint"]["checkpoint_sequence"]
        if sequence in by_sequence:
            if receipt["receipt_id"] == by_sequence[sequence]["receipt_id"]:
                raise AuditAnchorError("replayed audit anchor receipt")
            raise AuditAnchorError("forked audit anchor receipts share a checkpoint sequence")
        if receipt["receipt_id"] in receipt_ids:
            raise AuditAnchorError("replayed audit anchor receipt")
        by_sequence[sequence] = receipt
        receipt_ids.add(receipt["receipt_id"])

    ordered = [by_sequence[key] for key in sorted(by_sequence)]
    if [item["checkpoint"]["checkpoint_sequence"] for item in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        raise AuditAnchorError("audit checkpoint sequence is not contiguous from one")

    ledger_id = ordered[0]["checkpoint"]["ledger"]["ledger_id"]
    previous = None
    previous_entry_sequence = 0
    previous_signed_at = None
    previous_generation = 0
    for receipt in ordered:
        checkpoint = receipt["checkpoint"]
        if checkpoint["ledger"]["ledger_id"] != ledger_id:
            raise AuditAnchorError("audit anchor chain mixes ledger identities")
        if checkpoint["ledger"]["entry_sequence"] <= previous_entry_sequence:
            raise AuditAnchorError("audit anchor did not advance the ledger head")
        expected_previous = (
            {"receipt_id": None, "receipt_digest": None}
            if previous is None
            else {
                "receipt_id": previous["receipt_id"],
                "receipt_digest": object_digest(previous),
            }
        )
        if checkpoint["previous_anchor"] != expected_previous:
            raise AuditAnchorError("audit anchor predecessor link is broken or forked")
        signed_at = _parse_time(receipt["witness"]["signed_at"], "signed_at")
        if previous_signed_at is not None and signed_at < previous_signed_at:
            raise AuditAnchorError("audit witness time moved backwards")
        generation = receipt["revocation_snapshot"]["generation"]
        if generation < previous_generation:
            raise AuditAnchorError("audit witness revocation generation moved backwards")
        key_id = receipt["signing_key"]["key_id"]
        public_key = trusted_public_keys.get(key_id)
        if public_key is None:
            raise AuditAnchorError("audit receipt uses an untrusted witness key")
        verify_receipt(
            ledger_path,
            receipt,
            public_key,
            as_of=as_of,
            revoked_key_ids=revoked_key_ids,
        )
        previous = receipt
        previous_entry_sequence = checkpoint["ledger"]["entry_sequence"]
        previous_signed_at = signed_at
        previous_generation = generation

    return {
        "ledger_id": ledger_id,
        "checkpoint_count": len(ordered),
        "last_checkpoint_sequence": ordered[-1]["checkpoint"]["checkpoint_sequence"],
        "last_anchored_entry_sequence": previous_entry_sequence,
        "last_receipt_id": ordered[-1]["receipt_id"],
        "last_receipt_digest": object_digest(ordered[-1]),
        "verified_at": as_of,
    }


def load_json_object(path: Path | str) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditAnchorError(f"cannot read JSON object from {path}") from exc
    if not isinstance(value, dict):
        raise AuditAnchorError(f"{path} must contain a JSON object")
    return value
