"""Deterministic, advisory sourcing work derived from mission coverage gaps.

This module deliberately stops before acquisition.  It can shape and rank
candidate-discovery work, but it cannot authorize a source, fetch content,
sign an artifact, or publish a release.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Mapping

from corpus_factory.validator import ContractValidationError, validate_instance


WORK_PLAN_VERSION = "1.0.0"
MAX_WORK_ITEMS = 100
MAX_CANDIDATES_PER_ITEM = 5
MAX_DOCUMENT_PROPOSALS_PER_SOURCE = 3

_SAFETY_PRIORITY = {"advisory": 0, "standard": 1, "high": 2, "critical": 3}
_AUTHORITY_HINTS = {
    "critical": ["official"],
    "high": ["official", "delegated"],
    "standard": ["official", "delegated", "expert"],
    "advisory": ["official", "delegated", "expert", "community", "advisory"],
}
_MISSING_INTENT_PREFIX = "no classification supports intent: "


class SourcingPlanError(ValueError):
    """Raised when coverage evidence cannot safely produce sourcing work."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SourcingPlanError("work plan is not canonical JSON: {0}".format(exc)) from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate(record: Mapping[str, Any], record_type: str, label: str) -> None:
    try:
        validate_instance(record, record_type)
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        raise SourcingPlanError("invalid {0}: {1}".format(label, exc)) from exc


def _uncovered_intents(
    requirement: Mapping[str, Any], report_requirement: Mapping[str, Any]
) -> List[str]:
    required = set(requirement["required_intents"])
    uncovered = set()
    for gap in report_requirement["gaps"]:
        if gap["code"] != "MISSING_INTENT":
            continue
        message = gap["message"]
        if not message.startswith(_MISSING_INTENT_PREFIX):
            raise SourcingPlanError("coverage report has an unreadable MISSING_INTENT gap")
        intent = message[len(_MISSING_INTENT_PREFIX) :]
        if intent not in required:
            raise SourcingPlanError(
                "coverage report names an intent outside the mission requirement: " + intent
            )
        uncovered.add(intent)
    return sorted(uncovered)


def _check_report_alignment(
    mission: Mapping[str, Any], coverage_report: Mapping[str, Any]
) -> Dict[str, Mapping[str, Any]]:
    if coverage_report["mission_profile_id"] != mission["mission_profile_id"]:
        raise SourcingPlanError("mission_profile_id does not match coverage report")
    if coverage_report["event_id"] != mission["event"]["event_id"]:
        raise SourcingPlanError("event_id does not match coverage report")

    mission_requirements = {
        requirement["requirement_id"]: requirement
        for requirement in mission["required_information"]
    }
    report_requirements = {
        requirement["requirement_id"]: requirement
        for requirement in coverage_report["requirements"]
    }
    if set(report_requirements) != set(mission_requirements):
        raise SourcingPlanError("coverage report requirements do not match the mission")
    for requirement_id, requirement in mission_requirements.items():
        reported = report_requirements[requirement_id]
        if reported["category_id"] != requirement["category_id"]:
            raise SourcingPlanError(
                "coverage report category does not match mission requirement: "
                + requirement_id
            )
        if reported["safety_class"] != requirement["safety_class"]:
            raise SourcingPlanError(
                "coverage report safety class does not match mission requirement: "
                + requirement_id
            )
    return mission_requirements


def build_sourcing_work_items(
    mission: Mapping[str, Any],
    coverage_report: Mapping[str, Any],
    *,
    max_items: int = 25,
) -> Dict[str, Any]:
    """Turn coverage gaps into bounded, ranked, non-executing work items.

    The returned authority classes are conservative discovery hints, not source
    approvals.  A separate, human-governed stage must authorize every external
    action and release transition.
    """

    if not isinstance(mission, Mapping):
        raise SourcingPlanError("mission must be an object")
    if not isinstance(coverage_report, Mapping):
        raise SourcingPlanError("coverage report must be an object")
    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise SourcingPlanError("max_items must be an integer")
    if max_items < 1 or max_items > MAX_WORK_ITEMS:
        raise SourcingPlanError("max_items must be between 1 and {0}".format(MAX_WORK_ITEMS))

    _validate(mission, "corpus_mission_profile", "corpus mission profile")
    _validate(coverage_report, "coverage_report", "coverage report")
    mission_requirements = _check_report_alignment(mission, coverage_report)

    gap_reports = [
        requirement
        for requirement in coverage_report["requirements"]
        if requirement["status"] == "GAP"
    ]
    gap_reports.sort(
        key=lambda item: (
            -_SAFETY_PRIORITY[item["safety_class"]],
            item["category_id"],
            item["requirement_id"],
        )
    )

    work_items: List[Dict[str, Any]] = []
    for rank, report_requirement in enumerate(gap_reports[:max_items], start=1):
        requirement = mission_requirements[report_requirement["requirement_id"]]
        qualifying_sources = sorted(report_requirement["qualifying_source_ids"])
        item: Dict[str, Any] = {
            "record_type": "sourcing_work_item",
            "rank": rank,
            "requirement_id": requirement["requirement_id"],
            "category_id": requirement["category_id"],
            "description": requirement["description"],
            "safety_class": requirement["safety_class"],
            "required_intents": sorted(requirement["required_intents"]),
            "uncovered_intents": _uncovered_intents(requirement, report_requirement),
            "scope": copy.deepcopy(mission["scope"]),
            "gap_codes": sorted({gap["code"] for gap in report_requirement["gaps"]}),
            "source_target": {
                "minimum_sources": requirement["minimum_sources"],
                "qualifying_source_ids": qualifying_sources,
                "additional_sources_needed": max(
                    0, requirement["minimum_sources"] - len(qualifying_sources)
                ),
            },
            "selection_hints": {
                "allowed_authority_classes": list(
                    _AUTHORITY_HINTS[requirement["safety_class"]]
                ),
                "minimum_risk_classification": requirement["safety_class"],
                "unverified_sources_allowed": False,
            },
            "discovery_limits": {
                "maximum_candidate_sources": MAX_CANDIDATES_PER_ITEM,
                "maximum_document_proposals_per_source": MAX_DOCUMENT_PROPOSALS_PER_SOURCE,
                "authorized_network_requests": 0,
            },
            "human_gates": [
                "source_authorization",
                "content_fetch",
                "artifact_signing",
                "release_publication",
            ],
        }
        item["work_item_id"] = _digest(
            {
                "coverage_report_id": coverage_report["report_id"],
                "work_item": item,
            }
        )
        work_items.append(item)

    available = len(gap_reports)
    plan: Dict[str, Any] = {
        "schema_version": WORK_PLAN_VERSION,
        "record_type": "sourcing_work_plan",
        "mission_profile_id": mission["mission_profile_id"],
        "event_id": mission["event"]["event_id"],
        "coverage_report_id": coverage_report["report_id"],
        "as_of": coverage_report["as_of"],
        "summary": {
            "available_gap_requirements": available,
            "work_items": len(work_items),
            "omitted": available - len(work_items),
            "truncated": available > len(work_items),
        },
        "automation_boundary": {
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
        },
        "work_items": work_items,
    }
    plan["work_plan_id"] = _digest(plan)
    return plan

