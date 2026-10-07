"""Fail-closed governance evidence for corpus packaging and promotion."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping

from corpus_factory.validator import ContractValidationError, validate_instance


GOVERNED_PROFILE = "governed-v1"
LEGACY_PROFILE = "legacy-v1"
CONTRACT_PROFILES = (GOVERNED_PROFILE, LEGACY_PROFILE)


class GovernanceValidationError(ValueError):
    """Raised when release governance evidence is incomplete or inconsistent."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def object_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_equal(label: str, observed: Any, expected: Any) -> None:
    if observed != expected:
        raise GovernanceValidationError(
            f"{label} identity mismatch: expected {expected!r}, got {observed!r}"
        )


def validate_governance_evidence(
    *,
    event_id: str,
    mission_profile: Mapping[str, Any],
    source_registry: Mapping[str, Any],
    coverage_report: Mapping[str, Any],
    lineage_manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate a COVERED evidence set and return immutable package bindings."""

    try:
        validate_instance(mission_profile, "corpus_mission_profile")
        validate_instance(source_registry, "source_registry")
        validate_instance(coverage_report, "coverage_report")
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        raise GovernanceValidationError(f"governance contract validation failed: {exc}") from exc

    mission_id = mission_profile.get("mission_profile_id")
    registry_id = source_registry.get("registry_id")
    _require_equal("mission event", mission_profile.get("event", {}).get("event_id"), event_id)
    _require_equal("source registry event", source_registry.get("event_id"), event_id)
    _require_equal("coverage event", coverage_report.get("event_id"), event_id)
    _require_equal("coverage mission", coverage_report.get("mission_profile_id"), mission_id)
    _require_equal("coverage registry", coverage_report.get("registry_id"), registry_id)

    requirements = coverage_report.get("requirements")
    if coverage_report.get("decision") != "COVERED" or not isinstance(requirements, list):
        raise GovernanceValidationError("governed packaging requires a COVERED coverage report")
    if any(not isinstance(item, Mapping) or item.get("status") != "COVERED" for item in requirements):
        raise GovernanceValidationError(
            "governed packaging requires every mission requirement to be COVERED"
        )
    mission_requirements = mission_profile.get("required_information")
    if not isinstance(mission_requirements, list):
        raise GovernanceValidationError("mission required_information must be an array")
    mission_coverage = {
        item["requirement_id"]: (item["category_id"], item["safety_class"])
        for item in mission_requirements
    }
    report_coverage = {
        item["requirement_id"]: (item["category_id"], item["safety_class"])
        for item in requirements
    }
    if report_coverage != mission_coverage:
        raise GovernanceValidationError(
            "coverage report requirements do not exactly match the mission profile"
        )

    if lineage_manifest.get("record_type") != "corpus_lineage_manifest":
        raise GovernanceValidationError("lineage manifest has an unsupported record_type")
    _require_equal("lineage event", lineage_manifest.get("event_id"), event_id)
    lineage_registry = lineage_manifest.get("source_registry")
    if not isinstance(lineage_registry, Mapping):
        raise GovernanceValidationError("lineage manifest has no source_registry binding")
    _require_equal("lineage registry", lineage_registry.get("registry_id"), registry_id)
    registry_digest = object_digest(source_registry)
    _require_equal("lineage registry digest", lineage_registry.get("digest"), registry_digest)

    lineage_body = {
        key: value for key, value in lineage_manifest.items() if key != "manifest_digest"
    }
    expected_lineage_id = object_digest(lineage_body)
    _require_equal(
        "lineage manifest digest", lineage_manifest.get("manifest_digest"), expected_lineage_id
    )

    lineage_records = lineage_manifest.get("records")
    if not isinstance(lineage_records, list):
        raise GovernanceValidationError("lineage manifest records must be an array")
    lineage_classifications = set()
    for record in lineage_records:
        if not isinstance(record, Mapping):
            raise GovernanceValidationError("lineage manifest record must be an object")
        classification = record.get("classification")
        lifecycle = record.get("lifecycle")
        if not isinstance(classification, Mapping) or not isinstance(lifecycle, Mapping):
            raise GovernanceValidationError("lineage record lacks classification or lifecycle")
        if lifecycle.get("state") == "classified":
            _require_equal(
                "lineage classification mission",
                classification.get("mission_profile_id"),
                mission_id,
            )
            classification_id = classification.get("classification_id")
            if not isinstance(classification_id, str):
                raise GovernanceValidationError("classified lineage record has no classification_id")
            lineage_classifications.add(classification_id)

    covered_classifications = {
        classification_id
        for requirement in requirements
        for classification_id in requirement.get("qualifying_classification_ids", [])
    }
    missing = sorted(covered_classifications - lineage_classifications)
    if missing:
        raise GovernanceValidationError(
            "coverage report references classifications absent from lineage: " + ", ".join(missing)
        )

    return {
        "contract_profile": GOVERNED_PROFILE,
        "event_id": event_id,
        "mission_profile": {
            "mission_profile_id": mission_id,
            "digest": object_digest(mission_profile),
        },
        "source_registry": {
            "registry_id": registry_id,
            "digest": registry_digest,
        },
        "coverage_report": {
            "report_id": coverage_report["report_id"],
            "digest": object_digest(coverage_report),
        },
        "lineage_manifest": {
            "manifest_digest": lineage_manifest["manifest_digest"],
            "digest": object_digest(lineage_manifest),
        },
    }
