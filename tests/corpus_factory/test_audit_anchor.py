"""External witness tests for the Corpus Factory hash-chain audit ledger."""

from __future__ import annotations

import copy
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from corpus_factory.audit import append_audit_event
from corpus_factory.audit_anchor import (
    AuditAnchorError,
    finalize_receipt,
    object_digest,
    prepare_checkpoint,
    prepare_witness_statement,
    public_key_id,
    signing_payload,
    verify_anchor_chain,
)
from scripts.corpus_audit_anchor import main


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
LEDGER_ID = "corpus-factory-production-audit"
REQUESTER = "urn:example:service:corpus-factory"
WITNESS = "urn:example:service:external-audit-witness"


def _append(ledger, sequence):
    return append_audit_event(
        ledger,
        event_type="acquisition_completed" if sequence == 1 else "release_published",
        occurred_at=f"2026-10-06T12:0{sequence}:00Z",
        actor=REQUESTER,
        subject_id="source-shelter-primary",
        payload_digest=DIGEST_A if sequence == 1 else DIGEST_B,
    )


def _receipt(
    ledger,
    private_key,
    *,
    created_at,
    signed_at,
    previous=None,
    requester=REQUESTER,
):
    public_key = private_key.public_key()
    checkpoint = prepare_checkpoint(
        ledger,
        ledger_id=LEDGER_ID,
        requester_identity=requester,
        created_at=created_at,
        previous_receipt=previous,
        previous_public_key=public_key if previous else None,
    )
    statement = prepare_witness_statement(
        checkpoint,
        witness_identity=WITNESS,
        witness_organization="Independent Audit Witness Cooperative",
        signed_at=signed_at,
        public_key=public_key,
        key_valid_from="2026-10-01T00:00:00Z",
        key_expires_at="2027-01-01T00:00:00Z",
        revocation_generation=4,
        revocation_checked_at=created_at,
    )
    return finalize_receipt(
        statement, private_key.sign(signing_payload(statement)), public_key
    )


def _chain(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    private_key = Ed25519PrivateKey.generate()
    _append(ledger, 1)
    first = _receipt(
        ledger,
        private_key,
        created_at="2026-10-06T12:02:00Z",
        signed_at="2026-10-06T12:03:00Z",
    )
    _append(ledger, 2)
    second = _receipt(
        ledger,
        private_key,
        created_at="2026-10-06T12:04:00Z",
        signed_at="2026-10-06T12:05:00Z",
        previous=first,
    )
    return ledger, private_key, [first, second]


def test_two_external_receipts_verify_offline_and_advance_monotonically(tmp_path):
    ledger, private_key, receipts = _chain(tmp_path)
    key = private_key.public_key()

    result = verify_anchor_chain(
        ledger,
        receipts,
        {public_key_id(key): key},
        as_of="2026-10-06T13:00:00Z",
    )

    assert result["ledger_id"] == LEDGER_ID
    assert result["checkpoint_count"] == 2
    assert result["last_anchored_entry_sequence"] == 2
    assert receipts[1]["checkpoint"]["previous_anchor"] == {
        "receipt_id": receipts[0]["receipt_id"],
        "receipt_digest": object_digest(receipts[0]),
    }


def test_ledger_tamper_and_rollback_are_detected_against_anchor(tmp_path):
    ledger, private_key, receipts = _chain(tmp_path)
    keys = {public_key_id(private_key.public_key()): private_key.public_key()}
    original = ledger.read_bytes()
    ledger.write_bytes(original.replace(b"release_published", b"release_candidate"))
    with pytest.raises(AuditAnchorError, match="ledger verification|digest|contract"):
        verify_anchor_chain(ledger, receipts, keys, as_of="2026-10-06T13:00:00Z")

    ledger.write_bytes(original.splitlines(keepends=True)[0])
    with pytest.raises(AuditAnchorError, match="outside the audit ledger"):
        verify_anchor_chain(ledger, receipts, keys, as_of="2026-10-06T13:00:00Z")


def test_replay_and_forked_checkpoint_sequence_are_detected(tmp_path):
    ledger, private_key, receipts = _chain(tmp_path)
    key = private_key.public_key()
    keys = {public_key_id(key): key}
    with pytest.raises(AuditAnchorError, match="replayed"):
        verify_anchor_chain(
            ledger, [receipts[0], receipts[0]], keys, as_of="2026-10-06T13:00:00Z"
        )

    fork_checkpoint = copy.deepcopy(receipts[1]["checkpoint"])
    fork_checkpoint["requester_identity"] = "urn:example:service:other-factory"
    body = {key: value for key, value in fork_checkpoint.items() if key != "checkpoint_id"}
    fork_checkpoint["checkpoint_id"] = object_digest(body)
    fork_statement = prepare_witness_statement(
        fork_checkpoint,
        witness_identity=WITNESS,
        witness_organization="Independent Audit Witness Cooperative",
        signed_at="2026-10-06T12:06:00Z",
        public_key=key,
        key_valid_from="2026-10-01T00:00:00Z",
        key_expires_at="2027-01-01T00:00:00Z",
        revocation_generation=4,
        revocation_checked_at="2026-10-06T12:04:00Z",
    )
    fork = finalize_receipt(
        fork_statement, private_key.sign(signing_payload(fork_statement)), key
    )
    with pytest.raises(AuditAnchorError, match="forked"):
        verify_anchor_chain(
            ledger,
            [receipts[0], receipts[1], fork],
            keys,
            as_of="2026-10-06T13:00:00Z",
        )


def test_witness_must_be_external_trusted_and_not_revoked(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    _append(ledger, 1)
    private_key = Ed25519PrivateKey.generate()
    checkpoint = prepare_checkpoint(
        ledger,
        ledger_id=LEDGER_ID,
        requester_identity=REQUESTER,
        created_at="2026-10-06T12:02:00Z",
    )
    with pytest.raises(AuditAnchorError, match="independent"):
        prepare_witness_statement(
            checkpoint,
            witness_identity=REQUESTER,
            witness_organization="Not External",
            signed_at="2026-10-06T12:03:00Z",
            public_key=private_key.public_key(),
            key_valid_from="2026-10-01T00:00:00Z",
            key_expires_at="2027-01-01T00:00:00Z",
            revocation_generation=1,
            revocation_checked_at="2026-10-06T12:02:00Z",
        )

    receipt = _receipt(
        ledger,
        private_key,
        created_at="2026-10-06T12:02:00Z",
        signed_at="2026-10-06T12:03:00Z",
    )
    key_id = public_key_id(private_key.public_key())
    with pytest.raises(AuditAnchorError, match="revocation"):
        verify_anchor_chain(
            ledger,
            [receipt],
            {key_id: private_key.public_key()},
            as_of="2026-10-06T13:00:00Z",
            revoked_key_ids=[key_id],
        )


def test_tampered_witness_signature_fails_closed(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    _append(ledger, 1)
    private_key = Ed25519PrivateKey.generate()
    receipt = _receipt(
        ledger,
        private_key,
        created_at="2026-10-06T12:02:00Z",
        signed_at="2026-10-06T12:03:00Z",
    )
    receipt["signature"]["value"] = "A" * 86 + "=="
    key = private_key.public_key()

    with pytest.raises(AuditAnchorError, match="signature"):
        verify_anchor_chain(
            ledger,
            [receipt],
            {public_key_id(key): key},
            as_of="2026-10-06T13:00:00Z",
        )


def test_cli_prepare_finalize_verify_round_trip_without_private_key_input(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    _append(ledger, 1)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_path = tmp_path / "witness-public.pem"
    public_path.write_bytes(
        public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    statement = tmp_path / "statement.json"
    payload = tmp_path / "payload.bin"
    signature = tmp_path / "signature.bin"
    receipt = tmp_path / "receipt.json"

    assert main([
        "prepare", "--ledger", str(ledger), "--ledger-id", LEDGER_ID,
        "--requester-identity", REQUESTER, "--created-at", "2026-10-06T12:02:00Z",
        "--witness-identity", WITNESS, "--witness-organization", "External Witness",
        "--signed-at", "2026-10-06T12:03:00Z", "--witness-public-key", str(public_path),
        "--key-valid-from", "2026-10-01T00:00:00Z", "--key-expires-at", "2027-01-01T00:00:00Z",
        "--revocation-generation", "1", "--revocation-checked-at", "2026-10-06T12:02:00Z",
        "--output", str(statement), "--payload-output", str(payload),
    ]) == 0
    signature.write_bytes(private_key.sign(payload.read_bytes()))
    assert main([
        "finalize", "--statement", str(statement), "--signature", str(signature),
        "--witness-public-key", str(public_path), "--output", str(receipt),
    ]) == 0
    assert main([
        "verify", "--ledger", str(ledger), "--receipt", str(receipt),
        "--witness-public-key", str(public_path), "--as-of", "2026-10-06T13:00:00Z",
    ]) == 0
    assert b"PRIVATE KEY" not in payload.read_bytes()
    assert "PRIVATE KEY" not in statement.read_text(encoding="utf-8")


def test_release_events_share_the_same_append_only_ledger(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    entry = append_audit_event(
        ledger,
        event_type="release_signing_authorized",
        occurred_at="2026-10-06T12:00:00Z",
        actor=REQUESTER,
        subject_id="release-summit-2026.1",
        payload_digest=DIGEST_A,
    )
    assert entry["event_type"] == "release_signing_authorized"
