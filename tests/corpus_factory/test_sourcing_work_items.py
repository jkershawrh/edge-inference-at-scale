import copy
import json
from pathlib import Path

import pytest

from corpus_factory.coverage import plan_mission_coverage
from corpus_factory.sourcing import SourcingPlanError, build_sourcing_work_items
from scripts.plan_corpus_sourcing import main


ROOT = Path(__file__).resolve().parents[2]
VALID = ROOT / "corpus_factory" / "fixtures" / "valid"
SUMMIT = ROOT / "corpus_factory" / "examples" / "summit_connect"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _mission_and_report():
    mission = _load(VALID / "corpus-mission-profile.json")
    registry = _load(SUMMIT / "source-registry.json")
    classification = _load(VALID / "document-classification.json")
    classification["coverage"]["supported_intents"].append("agenda_change")
    registry["sources"][0]["source_id"] = "source-summit-schedule"
    report = plan_mission_coverage(
        mission,
        registry,
        [classification],
        as_of="2026-07-02T12:00:00-05:00",
    )
    return mission, report


def test_gap_work_items_are_deterministic_ranked_and_bounded():
    mission, report = _mission_and_report()

    first = build_sourcing_work_items(mission, report, max_items=4)
    second = build_sourcing_work_items(mission, report, max_items=4)

    assert first == second
    assert first["summary"] == {
        "available_gap_requirements": 10,
        "work_items": 4,
        "omitted": 6,
        "truncated": True,
    }
    assert [item["rank"] for item in first["work_items"]] == [1, 2, 3, 4]
    assert [item["safety_class"] for item in first["work_items"]] == [
        "critical",
        "high",
        "high",
        "standard",
    ]
    assert first["work_plan_id"].startswith("sha256:")


def test_work_item_is_category_intent_scope_authority_and_risk_aware():
    mission, report = _mission_and_report()

    plan = build_sourcing_work_items(mission, report)
    safety = next(
        item for item in plan["work_items"] if item["category_id"] == "safety_emergency"
    )

    assert safety["requirement_id"] == "summit-safety"
    assert safety["required_intents"] == [
        "emergency_contact",
        "first_aid",
        "safety_escalation",
    ]
    assert safety["uncovered_intents"] == safety["required_intents"]
    assert safety["scope"] == mission["scope"]
    assert safety["source_target"] == {
        "minimum_sources": 1,
        "qualifying_source_ids": [],
        "additional_sources_needed": 1,
    }
    assert safety["selection_hints"] == {
        "allowed_authority_classes": ["official"],
        "minimum_risk_classification": "critical",
        "unverified_sources_allowed": False,
    }
    assert safety["gap_codes"] == [
        "INSUFFICIENT_QUALIFYING_SOURCES",
        "MISSING_INTENT",
        "NO_QUALIFYING_CLASSIFICATION",
    ]


def test_work_plan_explicitly_prohibits_autonomous_execution_and_release_actions():
    mission, report = _mission_and_report()

    plan = build_sourcing_work_items(mission, report)

    assert plan["automation_boundary"] == {
        "advisory_only": True,
        "network_access": False,
        "may_discover_candidates": True,
        "may_rank_candidates": True,
        "may_propose_registry_changes": True,
        "may_fetch_content": False,
        "may_approve_sources": False,
        "may_sign_artifacts": False,
        "may_publish_releases": False,
        "human_authorization_required_for_execution": True,
    }
    assert all(
        item["human_gates"]
        == ["source_authorization", "content_fetch", "artifact_signing", "release_publication"]
        for item in plan["work_items"]
    )


def test_covered_and_conflicted_requirements_do_not_become_sourcing_work():
    mission, report = _mission_and_report()
    report = copy.deepcopy(report)
    schedule = next(
        item for item in report["requirements"] if item["category_id"] == "schedule"
    )

    plan = build_sourcing_work_items(mission, report)

    assert all(item["category_id"] != "schedule" for item in plan["work_items"])
    assert schedule["status"] == "COVERED"


def test_tampered_or_mismatched_reports_fail_closed():
    mission, report = _mission_and_report()
    tampered = copy.deepcopy(report)
    tampered["requirements"][0]["category_id"] = "other"

    with pytest.raises(SourcingPlanError, match="coverage report"):
        build_sourcing_work_items(mission, tampered)

    other = copy.deepcopy(mission)
    other["mission_profile_id"] = "another-mission"
    with pytest.raises(SourcingPlanError, match="mission_profile_id"):
        build_sourcing_work_items(other, report)


@pytest.mark.parametrize("maximum", [0, -1, True, 101])
def test_work_item_limit_is_strict(maximum):
    mission, report = _mission_and_report()

    with pytest.raises(SourcingPlanError, match="max_items"):
        build_sourcing_work_items(mission, report, max_items=maximum)


def test_cli_writes_gap_work_plan_and_returns_zero(tmp_path):
    mission, report = _mission_and_report()
    mission_path = tmp_path / "mission-profile.json"
    report_path = tmp_path / "coverage-report.json"
    output_path = tmp_path / "sourcing-work-plan.json"
    mission_path.write_text(json.dumps(mission), encoding="utf-8")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = main(
        [
            "--mission-profile",
            str(mission_path),
            "--coverage-report",
            str(report_path),
            "--max-items",
            "3",
            "--output",
            str(output_path),
        ]
    )

    work_plan = json.loads(output_path.read_text(encoding="utf-8"))
    assert result == 0
    assert work_plan["record_type"] == "sourcing_work_plan"
    assert work_plan["summary"] == {
        "available_gap_requirements": 10,
        "work_items": 3,
        "omitted": 7,
        "truncated": True,
    }
