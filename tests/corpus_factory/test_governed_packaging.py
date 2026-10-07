"""Contract and integration tests for governed corpus packaging."""

from __future__ import annotations

import copy
import json
from argparse import Namespace
from pathlib import Path

import pytest

from corpus_factory.governance import GovernanceValidationError, object_digest
from scripts.package_corpus import build_package


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "corpus_factory" / "fixtures" / "valid"


def _write(path: Path, value) -> str:
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def _governance_evidence(tmp_path: Path):
    mission = json.loads((FIXTURES / "corpus-mission-profile.json").read_text())
    mission["required_information"] = [mission["required_information"][0]]
    requirement = mission["required_information"][0]
    event_id = mission["event"]["event_id"]

    registry = json.loads((FIXTURES / "source-registry.json").read_text())
    registry["event_id"] = event_id

    classification_id = "classification-governed-release"
    coverage = {
        "schema_version": "1.0.0",
        "record_type": "coverage_report",
        "mission_profile_id": mission["mission_profile_id"],
        "event_id": event_id,
        "registry_id": registry["registry_id"],
        "as_of": "2026-10-06T12:00:00Z",
        "decision": "COVERED",
        "summary": {"requirements": 1, "covered": 1, "gaps": 0, "conflicted": 0},
        "requirements": [{
            "requirement_id": requirement["requirement_id"],
            "category_id": requirement["category_id"],
            "safety_class": requirement["safety_class"],
            "status": "COVERED",
            "classification_ids": [classification_id],
            "qualifying_classification_ids": [classification_id],
            "qualifying_source_ids": [registry["sources"][0]["source_id"]],
            "gaps": [],
            "conflicts": [],
            "recommended_actions": [],
        }],
        "source_findings": [],
        "conflicts": [],
        "automation_boundary": {
            "advisory_only": True,
            "network_access": False,
            "publishes_release": False,
            "human_approval_required": True,
        },
    }
    coverage["report_id"] = object_digest(coverage)

    lineage = {
        "schema_version": "1.0.0",
        "record_type": "corpus_lineage_manifest",
        "event_id": event_id,
        "source_registry": {
            "registry_id": registry["registry_id"],
            "digest": object_digest(registry),
        },
        "classification_set_digest": "sha256:" + "a" * 64,
        "records": [{
            "classification": {
                "classification_id": classification_id,
                "mission_profile_id": mission["mission_profile_id"],
            },
            "lifecycle": {"state": "classified"},
        }],
    }
    lineage["manifest_digest"] = object_digest(lineage)
    return mission, registry, coverage, lineage


def _args(tmp_path: Path, evidence) -> Namespace:
    mission, registry, coverage, lineage = evidence
    documents = tmp_path / "documents.json"
    documents.write_text(
        json.dumps([{"id": "event-facts", "text": "The event begins at 08:00."}]),
        encoding="utf-8",
    )
    return Namespace(
        event_id=mission["event"]["event_id"],
        event_name=mission["event"]["name"],
        version="governed-test",
        output_dir=str(tmp_path / "output"),
        created_at="2026-10-06T12:00:00Z",
        signing_key=None,
        input_documents=str(documents),
        contract_profile="governed-v1",
        mission_profile=_write(tmp_path / "mission.json", mission),
        source_registry=_write(tmp_path / "registry.json", registry),
        coverage_report=_write(tmp_path / "coverage.json", coverage),
        lineage_manifest=_write(tmp_path / "lineage.json", lineage),
    )


def test_governed_package_binds_exact_coverage_and_lineage_digests(tmp_path):
    evidence = _governance_evidence(tmp_path)
    package = build_package(_args(tmp_path, evidence))
    manifest = json.loads((package / "manifest.json").read_text())

    assert manifest["contract_profile"] == "governed-v1"
    assert manifest["governance"]["coverage_report"] == {
        "report_id": evidence[2]["report_id"],
        "digest": object_digest(evidence[2]),
    }
    assert manifest["governance"]["lineage_manifest"] == {
        "manifest_digest": evidence[3]["manifest_digest"],
        "digest": object_digest(evidence[3]),
    }


def test_governed_package_rejects_noncovered_report(tmp_path):
    evidence = list(_governance_evidence(tmp_path))
    coverage = copy.deepcopy(evidence[2])
    coverage["decision"] = "GAPS"
    coverage["summary"] = {"requirements": 1, "covered": 0, "gaps": 1, "conflicted": 0}
    coverage["requirements"][0]["status"] = "GAP"
    coverage["requirements"][0]["gaps"] = [{"code": "MISSING", "message": "Missing evidence"}]
    coverage["report_id"] = object_digest({k: v for k, v in coverage.items() if k != "report_id"})
    evidence[2] = coverage

    with pytest.raises(GovernanceValidationError, match="COVERED"):
        build_package(_args(tmp_path, evidence))


def test_governed_package_rejects_identity_or_lineage_drift(tmp_path):
    evidence = list(_governance_evidence(tmp_path))
    coverage = copy.deepcopy(evidence[2])
    coverage["registry_id"] = "registry-wrong-event"
    coverage["report_id"] = object_digest({k: v for k, v in coverage.items() if k != "report_id"})
    evidence[2] = coverage

    with pytest.raises(GovernanceValidationError, match="coverage registry identity mismatch"):
        build_package(_args(tmp_path, evidence))

    second = list(_governance_evidence(tmp_path))
    second[3]["records"][0]["classification"]["classification_id"] = "classification-tampered"
    with pytest.raises(GovernanceValidationError, match="lineage manifest digest"):
        build_package(_args(tmp_path, second))


def test_legacy_packaging_requires_explicit_profile(tmp_path):
    documents = tmp_path / "documents.json"
    documents.write_text(json.dumps([{"id": "notice", "text": "A notice."}]))
    args = Namespace(
        event_id="field-event",
        event_name="Field Event",
        version="v1",
        output_dir=str(tmp_path / "output"),
        created_at="2026-10-06T12:00:00Z",
        signing_key=None,
        input_documents=str(documents),
    )
    with pytest.raises(ValueError, match="explicitly select"):
        build_package(args)
