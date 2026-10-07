"""Deterministic contracts for corpus mission planning and classification.

Mission profiles state what a corpus pack must cover.  Document classifications
state how one canonical document may be used within that mission.  The builders
only assemble and validate records; they do not discover sources or approve
content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

from corpus_factory.validator import validate_instance


SCHEMA_VERSION = "2.0.0"


@dataclass(frozen=True)
class CoverageRequirementSpec:
    requirement_id: str
    category_id: str
    description: str
    required_intents: Tuple[str, ...]
    safety_class: str
    minimum_sources: int


@dataclass(frozen=True)
class MissionProfileSpec:
    profile_id: str
    version: str
    vertical: str
    mission: str
    event_id: str
    event_name: str
    event_start: str
    event_end: str
    timezone: str
    geographies: Tuple[str, ...]
    languages: Tuple[str, ...]
    audiences: Tuple[str, ...]
    accessibility_needs: Tuple[str, ...]
    delivery_channels: Tuple[str, ...]
    risk_level: str
    owner_organization: str
    approver_roles: Tuple[str, ...]
    valid_from: str
    valid_until: str
    expected_refresh_seconds: int
    required_information: Tuple[CoverageRequirementSpec, ...]


@dataclass(frozen=True)
class DocumentClassificationSpec:
    classification_id: str
    mission_profile_id: str
    document_id: str
    document_revision: int
    category_ids: Tuple[str, ...]
    supported_intents: Tuple[str, ...]
    geographies: Tuple[str, ...]
    languages: Tuple[str, ...]
    authority_class: str
    safety_class: str
    consequence_of_error: str
    valid_from: str
    valid_until: str | None
    stale_action: str
    source_ids: Tuple[str, ...]
    license: str
    redistribution: str
    verification_status: str
    verified_at: str | None
    reviewer_identity: str | None
    direct_answer_eligible: bool
    direct_answer_reason: str


def _unique(values: Tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(values))


def build_mission_profile(spec: MissionProfileSpec) -> Dict[str, Any]:
    """Build and validate a corpus mission profile without granting approval."""

    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "corpus_mission_profile",
        "mission_profile_id": spec.profile_id,
        "version": spec.version,
        "vertical": spec.vertical,
        "mission": spec.mission,
        "event": {
            "event_id": spec.event_id,
            "name": spec.event_name,
            "starts_at": spec.event_start,
            "ends_at": spec.event_end,
            "timezone": spec.timezone,
        },
        "scope": {
            "geographies": _unique(spec.geographies),
            "languages": _unique(spec.languages),
            "audiences": _unique(spec.audiences),
            "accessibility_needs": _unique(spec.accessibility_needs),
            "delivery_channels": _unique(spec.delivery_channels),
        },
        "governance": {
            "risk_level": spec.risk_level,
            "owner_organization": spec.owner_organization,
            "required_approver_roles": _unique(spec.approver_roles),
        },
        "validity": {
            "valid_from": spec.valid_from,
            "valid_until": spec.valid_until,
            "expected_refresh_seconds": spec.expected_refresh_seconds,
        },
        "required_information": [
            {
                "requirement_id": item.requirement_id,
                "category_id": item.category_id,
                "description": item.description,
                "required_intents": _unique(item.required_intents),
                "safety_class": item.safety_class,
                "minimum_sources": item.minimum_sources,
            }
            for item in spec.required_information
        ],
    }
    validate_instance(record, "corpus_mission_profile")
    return record


def build_document_classification(spec: DocumentClassificationSpec) -> Dict[str, Any]:
    """Build and validate a mission-scoped document usage classification."""

    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "document_classification",
        "classification_id": spec.classification_id,
        "mission_profile_id": spec.mission_profile_id,
        "document": {
            "document_id": spec.document_id,
            "revision": spec.document_revision,
        },
        "coverage": {
            "category_ids": _unique(spec.category_ids),
            "supported_intents": _unique(spec.supported_intents),
            "geographies": _unique(spec.geographies),
            "languages": _unique(spec.languages),
        },
        "authority": {"class": spec.authority_class},
        "risk": {
            "safety_class": spec.safety_class,
            "consequence_of_error": spec.consequence_of_error,
        },
        "validity": {
            "valid_from": spec.valid_from,
            "valid_until": spec.valid_until,
            "stale_action": spec.stale_action,
        },
        "provenance": {
            "source_ids": _unique(spec.source_ids),
            "license": spec.license,
            "redistribution": spec.redistribution,
        },
        "verification": {
            "status": spec.verification_status,
            "verified_at": spec.verified_at,
            "reviewer_identity": spec.reviewer_identity,
        },
        "answer_policy": {
            "direct_answer_eligible": spec.direct_answer_eligible,
            "reason": spec.direct_answer_reason,
        },
    }
    validate_instance(record, "document_classification")
    return record
