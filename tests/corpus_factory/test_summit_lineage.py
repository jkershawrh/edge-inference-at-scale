"""Integration tests for Summit Connect source-to-canonical lineage."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from corpus_factory.summit_lineage import (
    SummitLineageError,
    build_summit_lineage,
    render_summit_lineage,
    verify_summit_lineage,
)


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "summit_connect"
REGISTRY = ROOT / "corpus_factory" / "examples" / "summit_connect" / "source-registry.json"
CLASSIFICATIONS = (
    ROOT / "corpus_factory" / "examples" / "summit_connect" / "document-classifications.json"
)


def _record(manifest, document_id):
    return next(
        item for item in manifest["records"] if item["document"]["document_id"] == document_id
    )


def test_checked_in_summit_files_have_complete_explicit_lineage():
    manifest = build_summit_lineage(DATA, REGISTRY, CLASSIFICATIONS)

    assert len(manifest["records"]) == 7
    classified = [item for item in manifest["records"] if item["lifecycle"]["state"] == "classified"]
    excluded = [item for item in manifest["records"] if item["lifecycle"]["state"] == "excluded"]
    assert len(classified) == 6
    assert [item["document"]["document_id"] for item in excluded] == [
        "document-summit-architecture"
    ]
    assert excluded[0]["lifecycle"]["exclusion_reason"]

    schedule = _record(manifest, "document-summit-schedule")
    assert schedule["classification"]["classification_id"] == (
        "classification-summit-schedule-r1"
    )
    assert schedule["evidence"]["source_ids"] == ["source-summit-official-export"]
    assert schedule["evidence"]["path"] == "data/summit_connect/schedule.json"
    assert schedule["lifecycle"]["approval_state"] == "not_evaluated"


def test_build_is_byte_deterministic_and_verifiable():
    first = build_summit_lineage(DATA, REGISTRY, CLASSIFICATIONS)
    second = build_summit_lineage(DATA, REGISTRY, CLASSIFICATIONS)

    assert render_summit_lineage(first) == render_summit_lineage(second)
    assert first["manifest_digest"] == second["manifest_digest"]
    verify_summit_lineage(first, DATA, REGISTRY, CLASSIFICATIONS)


def test_exact_evidence_bytes_and_canonical_json_have_separate_identities(tmp_path):
    data_dir = tmp_path / "summit_connect"
    data_dir.mkdir()
    for source in DATA.glob("*.json"):
        (data_dir / source.name).write_bytes(source.read_bytes())

    before = build_summit_lineage(data_dir, REGISTRY, CLASSIFICATIONS)
    schedule_path = data_dir / "schedule.json"
    parsed = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule_path.write_text(json.dumps(parsed, indent=4) + "\n", encoding="utf-8")
    after = build_summit_lineage(data_dir, REGISTRY, CLASSIFICATIONS)

    before_schedule = _record(before, "document-summit-schedule")
    after_schedule = _record(after, "document-summit-schedule")
    assert before_schedule["evidence"]["digest"] != after_schedule["evidence"]["digest"]
    assert (
        before_schedule["document"]["canonical_digest"]
        == after_schedule["document"]["canonical_digest"]
    )


def test_unclassified_file_fails_closed(tmp_path):
    data_dir = tmp_path / "summit_connect"
    data_dir.mkdir()
    for source in DATA.glob("*.json"):
        (data_dir / source.name).write_bytes(source.read_bytes())
    (data_dir / "surprise.json").write_text('{"claim":"unreviewed"}\n', encoding="utf-8")

    with pytest.raises(SummitLineageError, match="neither a document classification"):
        build_summit_lineage(data_dir, REGISTRY, CLASSIFICATIONS)


def test_dangling_classification_and_unapproved_source_fail_closed(tmp_path):
    classification_document = json.loads(CLASSIFICATIONS.read_text(encoding="utf-8"))
    dangling = copy.deepcopy(classification_document)
    missing = copy.deepcopy(dangling["classifications"][0])
    missing["classification_id"] = "classification-summit-missing-r1"
    missing["document"]["document_id"] = "document-summit-missing"
    dangling["classifications"].append(missing)
    dangling_path = tmp_path / "dangling.json"
    dangling_path.write_text(json.dumps(dangling), encoding="utf-8")
    with pytest.raises(SummitLineageError, match="missing Summit documents"):
        build_summit_lineage(DATA, REGISTRY, dangling_path)

    unknown_source = copy.deepcopy(classification_document)
    unknown_source["classifications"][0]["provenance"]["source_ids"] = ["unknown-source"]
    unknown_path = tmp_path / "unknown-source.json"
    unknown_path.write_text(json.dumps(unknown_source), encoding="utf-8")
    with pytest.raises(SummitLineageError, match="unapproved source"):
        build_summit_lineage(DATA, REGISTRY, unknown_path)


def test_tampered_manifest_fails_verification():
    manifest = build_summit_lineage(DATA, REGISTRY, CLASSIFICATIONS)
    tampered = copy.deepcopy(manifest)
    tampered["records"][0]["evidence"]["byte_size"] += 1

    with pytest.raises(SummitLineageError, match="does not match current evidence"):
        verify_summit_lineage(tampered, DATA, REGISTRY, CLASSIFICATIONS)
