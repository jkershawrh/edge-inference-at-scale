import copy
import hashlib
import json
from pathlib import Path

import pytest

from corpus_factory.coverage import CoveragePlanError, plan_coverage, plan_mission_coverage
from corpus_factory.validator import event_policy_subject_digest
from scripts.plan_corpus_coverage import main


ROOT = Path(__file__).resolve().parents[2]
VALID = ROOT / "corpus_factory" / "fixtures" / "valid"


def _load(name):
    return json.loads((VALID / name).read_text(encoding="utf-8"))


def _policy():
    return _load("event-policy.json")


def _registry():
    registry = _load("source-registry.json")
    source = copy.deepcopy(registry["sources"][0])
    source["source_id"] = "source-shelter-secondary"
    source["connector"]["connector_id"] = "connector-region4-shelters-secondary"
    source["connector"]["url"] = "https://alerts.example.gov/region4/shelters-secondary.json"
    registry["sources"].append(source)
    return registry


def _document(document_id="doc-critical-status", fact_value="open"):
    source = _load("canonical-document.json")
    text = "North shelter status: {0}.".format(fact_value)
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    citation = {
        "source_id": "source-shelter-primary",
        "evidence_digest": "sha256:" + "a" * 64,
        "location": "$.status",
    }
    source.update(
        {
            "document_id": document_id,
            "document_digest": digest,
            "event_id": "field-event-2026",
            "deployment_ids": ["site-north"],
            "scope": {
                "geographies": ["region-north"],
                "languages": ["en"],
                "audiences": ["public"],
            },
            "canonical_text": text,
            "structured_facts": [
                {"fact_id": "shelter-status", "value": fact_value, "citations": [citation]}
            ],
            "source_citations": [citation],
            "validity": {
                "valid_from": "2026-10-06T00:00:00Z",
                "valid_until": "2026-10-07T00:00:00Z",
                "review_due_at": "2026-10-06T23:00:00Z",
                "stale_action": "block",
            },
            "conflict": {"state": "none", "explanation": "", "supersedes": []},
            "safety_class": "critical",
            "permitted_delivery_channels": ["sms", "lora"],
            "approval_state": "in_review",
        }
    )
    return source


def _refresh_policy_digest(policy):
    digest = event_policy_subject_digest(policy)
    for approval in policy["human_approval_attestations"]:
        approval["policy_digest"] = digest


def _summit_inputs():
    mission = _load("corpus-mission-profile.json")
    classification = _load("document-classification.json")
    classification["coverage"]["supported_intents"].append("agenda_change")
    registry = _registry()
    registry["event_id"] = "summit-connect-2026"
    registry["registry_id"] = "registry-summit-connect-2026"
    source = registry["sources"][0]
    source["source_id"] = "source-summit-schedule"
    source["connector"]["connector_id"] = "connector-summit-schedule"
    source["connector"]["url"] = "https://alerts.example.gov/summit/schedule.json"
    source["scope"] = {
        "geographies": ["summit-city", "convention-center"],
        "languages": ["en"],
        "audiences": ["attendees", "speakers", "staff"],
        "subjects": ["event_identity", "schedule"],
    }
    source["freshness"].update(
        {
            "effective_from": "2026-07-01T00:00:00-05:00",
            "valid_until": "2026-07-18T23:59:59-05:00",
        }
    )
    registry["sources"] = [source]
    return mission, registry, classification


def test_plan_is_deterministic_and_identifies_document_work():
    first = plan_coverage(_policy(), _registry(), [], as_of="2026-10-06T12:00:00Z")
    second = plan_coverage(_policy(), _registry(), [], as_of="2026-10-06T12:00:00Z")

    assert first == second
    assert first["decision"] == "GAPS"
    assert first["summary"] == {
        "requirements": 2,
        "covered": 0,
        "gaps": 2,
        "conflicted": 0,
    }
    critical = next(item for item in first["requirements"] if item["requirement_id"] == "coverage-critical-status")
    assert critical["candidate_source_ids"] == ["source-shelter-primary", "source-shelter-secondary"]
    assert {action["action"] for action in critical["recommended_actions"]} == {"ACQUIRE_AND_CLASSIFY"}
    assert first["plan_id"].startswith("sha256:")


def test_source_must_match_scope_authority_rights_and_enabled_state():
    registry = _registry()
    registry["sources"][0]["scope"]["geographies"] = ["region-south"]
    registry["sources"][1]["rights"]["redistribution"] = "prohibited"
    registry["sources"][1]["approval"].update(
        {"status": "candidate", "reviewer_identity": None, "reviewed_at": None}
    )

    report = plan_coverage(_policy(), registry, [], as_of="2026-10-06T12:00:00Z")
    critical = next(item for item in report["requirements"] if item["requirement_id"] == "coverage-critical-status")

    assert critical["candidate_source_ids"] == []
    assert {gap["code"] for gap in critical["gaps"]} >= {
        "NO_CANDIDATE_SOURCE",
        "INSUFFICIENT_INDEPENDENT_SOURCES",
    }
    assert any(item["code"] == "SOURCE_SCOPE_MISMATCH" for item in report["source_findings"])
    assert any(item["code"] == "REDISTRIBUTION_PROHIBITED" for item in report["source_findings"])


def test_conflicting_fact_values_block_requirement():
    one = _document("doc-status-a", "open")
    two = _document("doc-status-b", "closed")
    two["source_citations"][0]["source_id"] = "source-shelter-secondary"
    two["structured_facts"][0]["citations"][0]["source_id"] = "source-shelter-secondary"

    report = plan_coverage(_policy(), _registry(), [one, two], as_of="2026-10-06T12:00:00Z")
    critical = next(item for item in report["requirements"] if item["requirement_id"] == "coverage-critical-status")

    assert report["decision"] == "CONFLICTS"
    assert critical["status"] == "CONFLICTED"
    assert critical["conflicts"][0]["fact_id"] == "shelter-status"
    assert critical["conflicts"][0]["document_ids"] == ["doc-status-a", "doc-status-b"]
    assert critical["recommended_actions"][0]["action"] == "HUMAN_CONFLICT_REVIEW"


def test_explicit_contested_document_is_reported_even_without_fact_value_collision():
    document = _document()
    document["conflict"] = {
        "state": "contested",
        "explanation": "Two organizers disagree.",
        "supersedes": [],
    }

    report = plan_coverage(_policy(), _registry(), [document], as_of="2026-10-06T12:00:00Z")

    assert report["decision"] == "CONFLICTS"
    assert any(conflict["kind"] == "DECLARED_CONTESTED" for conflict in report["conflicts"])


def test_policy_and_registry_must_name_same_event():
    registry = _registry()
    registry["event_id"] = "another-event"

    with pytest.raises(CoveragePlanError, match="event_id"):
        plan_coverage(_policy(), registry, [], as_of="2026-10-06T12:00:00Z")


def test_invalid_inputs_fail_closed():
    policy = _policy()
    policy["coverage_requirements"][0]["scope"]["languages"] = ["fr"]
    _refresh_policy_digest(policy)

    with pytest.raises(CoveragePlanError, match="event policy"):
        plan_coverage(policy, _registry(), [], as_of="2026-10-06T12:00:00Z")


def test_summit_mission_planner_exposes_bounded_initial_gaps():
    mission, registry, classification = _summit_inputs()

    report = plan_mission_coverage(
        mission,
        registry,
        [classification],
        as_of="2026-07-02T12:00:00-05:00",
    )

    assert report["decision"] == "GAPS"
    assert report["summary"] == {
        "requirements": 11,
        "covered": 1,
        "gaps": 10,
        "conflicted": 0,
    }
    schedule = next(item for item in report["requirements"] if item["category_id"] == "schedule")
    assert schedule["status"] == "COVERED"
    assert report["automation_boundary"]["publishes_release"] is False


def test_cli_writes_gap_report_and_returns_one(tmp_path):
    mission, registry, classification = _summit_inputs()
    mission_path = tmp_path / "mission.json"
    registry_path = tmp_path / "registry.json"
    classifications_path = tmp_path / "classifications.json"
    output_path = tmp_path / "coverage-report.json"
    mission_path.write_text(json.dumps(mission), encoding="utf-8")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    classifications_path.write_text(
        json.dumps({"classifications": [classification]}), encoding="utf-8"
    )

    result = main(
        [
            "--mission-profile", str(mission_path),
            "--registry", str(registry_path),
            "--classifications", str(classifications_path),
            "--as-of", "2026-07-02T12:00:00-05:00",
            "--output", str(output_path),
        ]
    )

    assert result == 1
    assert json.loads(output_path.read_text())["decision"] == "GAPS"
