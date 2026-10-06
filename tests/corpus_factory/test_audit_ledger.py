"""Hash-chain and append-only behavior for Corpus Factory audit evidence."""

import json

import pytest

from corpus_factory.audit import AuditLedgerError, append_audit_event, verify_audit_ledger


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _append(path, event_type, occurred_at, payload):
    return append_audit_event(
        path,
        event_type=event_type,
        occurred_at=occurred_at,
        actor="urn:example:factory-worker",
        subject_id="source-shelter-primary",
        payload_digest=payload,
    )


def test_append_and_verify_chained_events(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    first = _append(ledger, "acquisition_completed", "2026-10-06T12:00:00Z", DIGEST_A)
    second = _append(ledger, "source_changed", "2026-10-06T12:01:00Z", DIGEST_B)
    summary = verify_audit_ledger(ledger)
    assert first["sequence"] == 1
    assert second["previous_entry_digest"] == first["entry_digest"]
    assert summary["entries"] == 2
    assert summary["last_entry_digest"] == second["entry_digest"]
    for raw in ledger.read_bytes().splitlines():
        assert json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode() == raw


def test_modified_entry_breaks_chain(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    _append(ledger, "acquisition_completed", "2026-10-06T12:00:00Z", DIGEST_A)
    data = ledger.read_text(encoding="utf-8").replace("acquisition_completed", "source_unchanged")
    ledger.write_text(data, encoding="utf-8")
    with pytest.raises(AuditLedgerError, match="digest"):
        verify_audit_ledger(ledger)


def test_incomplete_final_line_and_backwards_time_are_rejected(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    _append(ledger, "acquisition_completed", "2026-10-06T12:00:00Z", DIGEST_A)
    with pytest.raises(AuditLedgerError, match="backwards"):
        _append(ledger, "source_unchanged", "2026-10-06T11:59:59Z", DIGEST_B)
    ledger.write_bytes(ledger.read_bytes().rstrip(b"\n"))
    with pytest.raises(AuditLedgerError, match="incomplete"):
        verify_audit_ledger(ledger)


def test_invalid_event_never_appends(tmp_path):
    ledger = tmp_path / "audit.jsonl"
    with pytest.raises(AuditLedgerError, match="unknown"):
        _append(ledger, "arbitrary_event", "2026-10-06T12:00:00Z", DIGEST_A)
    assert ledger.read_bytes() == b""
