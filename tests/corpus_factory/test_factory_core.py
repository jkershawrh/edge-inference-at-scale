"""Focused offline tests for deterministic factory construction."""

from dataclasses import replace

import pytest

from corpus_factory.core import CanonicalDocumentSpec, SourceRecordSpec, build_from_file
from corpus_factory.validator import validate_instance


def _source_spec() -> SourceRecordSpec:
    return SourceRecordSpec(
        source_id="source-relief-shelters",
        publisher_id="relief-agency",
        publisher_name="Relief Agency",
        steward_identity="steward@example.test",
        steward_role="emergency information lead",
        authority_class="official",
        connector_id="manual-intake",
        acquisition_policy_version="2.0",
        acquired_at="2026-10-06T12:00:00Z",
        last_verified_at="2026-10-06T12:00:00Z",
        license="CC-BY-4.0",
        redistribution="permitted",
        geographies=("county-a",),
        languages=("en",),
        audiences=("public",),
        subjects=("shelter",),
        effective_from="2026-10-06T12:00:00Z",
        valid_until="2026-10-08T12:00:00Z",
        expected_refresh_seconds=3600,
        stale_action="block",
    )


def _document_spec() -> CanonicalDocumentSpec:
    return CanonicalDocumentSpec(
        document_id="document-relief-shelters",
        event_id="flood-2026",
        deployment_ids=("site-north",),
        geographies=("county-a",),
        languages=("en",),
        audiences=("public",),
        valid_from="2026-10-06T12:00:00Z",
        valid_until="2026-10-08T12:00:00Z",
        review_due_at="2026-10-07T12:00:00Z",
        stale_action="block",
        authority_rank=95,
        safety_class="critical",
        permitted_delivery_channels=("sms", "lora"),
        review_attestation_ids=("review-001",),
        approval_state="approved",
    )


def _build(source, root, store, maximum=42):
    return build_from_file(
        source,
        allowlisted_roots=[root],
        evidence_store=store,
        source_spec=_source_spec(),
        document_spec=_document_spec(),
        max_code_points=maximum,
    )


def test_rebuild_is_byte_for_byte_deterministic(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "shelters.md"
    source.write_text("# Shelters\nNorth school is open.\n\n## Hours\nOpen all day.\n", encoding="utf-8")

    first = _build(source, inputs, tmp_path / "evidence")
    second = _build(source.as_uri(), inputs, tmp_path / "evidence")

    assert first.source_record == second.source_record
    assert first.canonical_document == second.canonical_document
    assert first.chunks == second.chunks
    assert first.evidence_path == second.evidence_path
    for record in (first.source_record, first.canonical_document, *first.chunks):
        validate_instance(record)


def test_changed_evidence_creates_new_content_addressed_snapshot(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "notice.txt"
    source.write_text("Shelter A is open.", encoding="utf-8")
    first = _build(source, inputs, tmp_path / "evidence")

    source.write_text("Shelter A is closed.", encoding="utf-8")
    second = _build(source, inputs, tmp_path / "evidence")

    assert first.source_record["evidence"]["digest"] != second.source_record["evidence"]["digest"]
    assert first.source_record["source_id"] == second.source_record["source_id"]
    assert first.canonical_document["document_id"] == second.canonical_document["document_id"]
    assert first.canonical_document["document_digest"] != second.canonical_document["document_digest"]
    assert first.evidence_path != second.evidence_path
    assert first.evidence_path.read_text(encoding="utf-8") == "Shelter A is open."
    assert second.evidence_path.read_text(encoding="utf-8") == "Shelter A is closed."


def test_chunk_spans_and_section_paths_trace_exact_parent_text(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "water.md"
    source.write_text(
        "# Water\nBoil tap water before drinking.\n\n## Distribution\nPickup is at the clinic courtyard.\n",
        encoding="utf-8",
    )
    result = _build(source, inputs, tmp_path / "evidence", maximum=35)
    parent = result.canonical_document["canonical_text"]

    for chunk in result.chunks:
        span = chunk["source_span"]
        assert chunk["content"] == parent[span["start"] : span["end"]]
    distribution = next(chunk for chunk in result.chunks if "Distribution" in chunk["content"])
    assert distribution["section_path"] == ["Water", "Distribution"]


def test_non_allowlisted_source_is_rejected(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    source = outside / "untrusted.txt"
    source.write_text("Unreviewed claim", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside the allowlisted roots"):
        _build(source, allowed, tmp_path / "evidence")


def test_invalid_governance_metadata_fails_the_existing_contract(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "notice.txt"
    source.write_text("Verified notice", encoding="utf-8")
    bad_spec = replace(_source_spec(), authority_class="self-declared")

    with pytest.raises(ValueError):
        build_from_file(
            source,
            allowlisted_roots=[inputs],
            evidence_store=tmp_path / "evidence",
            source_spec=bad_spec,
            document_spec=_document_spec(),
        )
