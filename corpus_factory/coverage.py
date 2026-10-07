"""Deterministic, advisory coverage planning for a declared corpus mission.

The planner consumes already-classified contract records.  It does not fetch
content, mutate a registry, approve documents, or publish a release.  Its
recommendations are bounded work items for a human-governed corpus factory.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from corpus_factory.validator import ContractValidationError, validate_instance


REPORT_VERSION = "1.0.0"
SAFETY_RANK = {"advisory": 0, "standard": 1, "high": 2, "critical": 3}


class CoveragePlanError(ValueError):
    """Raised when validated inputs cannot form a trustworthy plan."""


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
        raise CoveragePlanError("input is not canonical JSON: {0}".format(exc)) from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CoveragePlanError("as_of must be an ISO-8601 time") from exc
    if parsed.tzinfo is None:
        raise CoveragePlanError("as_of must include a timezone")
    return parsed


def _validate(record: Mapping[str, Any], record_type: str, label: str) -> None:
    try:
        validate_instance(record, record_type)
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        raise CoveragePlanError("invalid {0}: {1}".format(label, exc)) from exc


def _active_at(window: Mapping[str, Any], as_of: datetime, start_key: str) -> bool:
    try:
        start = datetime.fromisoformat(str(window[start_key]).replace("Z", "+00:00"))
        raw_end = window.get("valid_until")
        end = (
            datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            if raw_end is not None
            else None
        )
    except (KeyError, TypeError, ValueError):
        return False
    return start <= as_of and (end is None or as_of < end)


def _scope_intersections(requirement: Mapping[str, Any]) -> List[Dict[str, str]]:
    scope = requirement["scope"]
    return [
        {
            "geography": geography,
            "language": language,
            "audience": audience,
            "channel": channel,
        }
        for geography, language, audience, channel in itertools.product(
            sorted(scope["geographies"]),
            sorted(scope["languages"]),
            sorted(scope["audiences"]),
            sorted(scope["delivery_channels"]),
        )
    ]


def _source_authorized(
    source: Mapping[str, Any], policy: Mapping[str, Any]
) -> bool:
    publisher_id = source["publisher"]["id"]
    return any(
        source["source_id"] in authority["source_ids"]
        and publisher_id in authority["publisher_ids"]
        and source["authority_class"] == authority["authority_class"]
        and set(source["scope"]["subjects"]).issubset(authority["subjects"])
        and set(source["scope"]["geographies"]).issubset(authority["geographies"])
        for authority in policy["authority_registry"]
    )


def _source_findings(
    source: Mapping[str, Any], policy: Mapping[str, Any], as_of: datetime
) -> List[Dict[str, str]]:
    source_id = source["source_id"]
    findings: List[Dict[str, str]] = []
    if not source["enabled"]:
        findings.append(
            {"code": "SOURCE_DISABLED", "source_id": source_id, "message": "source is disabled"}
        )
    approval = source.get("approval")
    if isinstance(approval, Mapping) and approval.get("status") != "approved":
        findings.append(
            {
                "code": "SOURCE_NOT_APPROVED",
                "source_id": source_id,
                "message": "source classification has not received human approval",
            }
        )
    if not _source_authorized(source, policy):
        findings.append(
            {
                "code": "SOURCE_NOT_AUTHORIZED",
                "source_id": source_id,
                "message": "source classification is not authorized by the mission policy",
            }
        )
    if source["rights"]["redistribution"] == "prohibited":
        findings.append(
            {
                "code": "REDISTRIBUTION_PROHIBITED",
                "source_id": source_id,
                "message": "source content cannot be redistributed in a corpus pack",
            }
        )
    if not _active_at(source["freshness"], as_of, "effective_from"):
        findings.append(
            {
                "code": "SOURCE_OUTSIDE_VALIDITY",
                "source_id": source_id,
                "message": "source is not valid at planning time",
            }
        )
    return findings


def _source_scope_matches(
    source: Mapping[str, Any], requirement: Mapping[str, Any]
) -> bool:
    source_scope = source["scope"]
    required_scope = requirement["scope"]
    return all(
        set(required_scope[field]).issubset(source_scope[field])
        for field in ("geographies", "languages", "audiences")
    )


def _source_subject_matches(
    source: Mapping[str, Any], requirement: Mapping[str, Any]
) -> bool:
    required_facts = set(requirement["selector"]["required_fact_ids"])
    return bool(required_facts.intersection(source["scope"]["subjects"]))


def _document_relevant(
    document: Mapping[str, Any], requirement: Mapping[str, Any]
) -> bool:
    selector = requirement["selector"]
    fact_ids = {
        fact["fact_id"]
        for fact in document["structured_facts"]
        if isinstance(fact, Mapping) and isinstance(fact.get("fact_id"), str)
    }
    return (
        document["document_id"] in selector["required_document_ids"]
        or bool(fact_ids.intersection(selector["required_fact_ids"]))
    )


def _document_covers_intersection(
    document: Mapping[str, Any], intersection: Mapping[str, str]
) -> bool:
    scope = document["scope"]
    return (
        intersection["geography"] in scope["geographies"]
        and intersection["language"] in scope["languages"]
        and intersection["audience"] in scope["audiences"]
        and intersection["channel"] in document["permitted_delivery_channels"]
    )


def _document_source_ids(document: Mapping[str, Any]) -> Set[str]:
    return {citation["source_id"] for citation in document["source_citations"]}


def _find_conflicts(documents: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    for document in documents:
        if document["conflict"]["state"] == "contested":
            conflicts.append(
                {
                    "kind": "DECLARED_CONTESTED",
                    "document_ids": [document["document_id"]],
                    "explanation": document["conflict"]["explanation"],
                }
            )

    facts: Dict[str, Dict[str, Set[str]]] = {}
    for document in documents:
        for fact in document["structured_facts"]:
            encoded = _canonical_json(fact["value"])
            facts.setdefault(fact["fact_id"], {}).setdefault(encoded, set()).add(
                document["document_id"]
            )
    for fact_id, values in sorted(facts.items()):
        if len(values) > 1:
            conflicts.append(
                {
                    "kind": "FACT_VALUE_COLLISION",
                    "fact_id": fact_id,
                    "document_ids": sorted(
                        {document_id for ids in values.values() for document_id in ids}
                    ),
                    "value_digests": sorted(_digest(json.loads(value)) for value in values),
                    "explanation": "classified documents assert different values for one fact",
                }
            )
    return sorted(
        conflicts,
        key=lambda item: (
            item["kind"],
            item.get("fact_id", ""),
            tuple(item["document_ids"]),
        ),
    )


def _gap(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def _action(action: str, reason: str) -> Dict[str, str]:
    return {"action": action, "reason": reason}


def plan_coverage(
    policy: Mapping[str, Any],
    registry: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> Dict[str, Any]:
    """Produce a deterministic coverage, gap, conflict, and work-item report."""

    if not isinstance(policy, Mapping):
        raise CoveragePlanError("policy must be an object")
    if not isinstance(registry, Mapping):
        raise CoveragePlanError("registry must be an object")
    if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
        raise CoveragePlanError("documents must be a sequence")
    planning_time = _parse_time(as_of)
    _validate(policy, "event_policy", "event policy")
    _validate(registry, "source_registry", "source registry")
    if policy["event_id"] != registry["event_id"]:
        raise CoveragePlanError("policy and source registry event_id values do not match")

    validated_documents: List[Mapping[str, Any]] = []
    identities: Set[Tuple[str, int]] = set()
    for index, document in enumerate(documents):
        if not isinstance(document, Mapping):
            raise CoveragePlanError("document {0} must be an object".format(index))
        _validate(document, "canonical_document", "document {0}".format(index))
        if document["event_id"] != policy["event_id"]:
            raise CoveragePlanError("document {0} event_id does not match policy".format(index))
        identity = (document["document_id"], document["revision"])
        if identity in identities:
            raise CoveragePlanError("duplicate document revision: {0}@{1}".format(*identity))
        identities.add(identity)
        validated_documents.append(document)

    findings_by_source: Dict[str, List[Dict[str, str]]] = {}
    all_source_findings: List[Dict[str, str]] = []
    for source in sorted(registry["sources"], key=lambda item: item["source_id"]):
        findings = _source_findings(source, policy, planning_time)
        findings_by_source[source["source_id"]] = findings
        all_source_findings.extend(findings)

    requirement_reports: List[Dict[str, Any]] = []
    all_conflicts: List[Dict[str, Any]] = []
    for requirement in sorted(
        policy["coverage_requirements"], key=lambda item: item["requirement_id"]
    ):
        requirement_id = requirement["requirement_id"]
        related_documents = [
            document
            for document in validated_documents
            if _document_relevant(document, requirement)
        ]
        conflicts = _find_conflicts(related_documents)
        for conflict in conflicts:
            all_conflicts.append({"requirement_id": requirement_id, **conflict})

        cited_source_ids = {
            source_id
            for document in related_documents
            for source_id in _document_source_ids(document)
        }
        candidate_sources = []
        scoped_but_unusable = []
        for source in registry["sources"]:
            inferred_match = _source_subject_matches(source, requirement)
            cited_match = source["source_id"] in cited_source_ids
            if not (inferred_match or cited_match):
                continue
            if not _source_scope_matches(source, requirement):
                scoped_but_unusable.append(
                    {
                        "code": "SOURCE_SCOPE_MISMATCH",
                        "source_id": source["source_id"],
                        "message": "source does not cover the full requirement geography, language, and audience scope",
                    }
                )
                continue
            if source["authority_class"] not in requirement["allowed_authority_classes"]:
                scoped_but_unusable.append(
                    {
                        "code": "AUTHORITY_CLASS_NOT_ALLOWED",
                        "source_id": source["source_id"],
                        "message": "source authority class is not allowed for this requirement",
                    }
                )
                continue
            if findings_by_source[source["source_id"]]:
                continue
            candidate_sources.append(source)
        all_source_findings.extend(scoped_but_unusable)

        qualifying_documents = []
        intersections = _scope_intersections(requirement)
        for document in related_documents:
            if document["approval_state"] != "approved":
                continue
            if document["conflict"]["state"] == "contested":
                continue
            if SAFETY_RANK[document["safety_class"]] < SAFETY_RANK[requirement["safety_class"]]:
                continue
            if not _active_at(document["validity"], planning_time, "valid_from"):
                continue
            if not all(
                _document_covers_intersection(document, intersection)
                for intersection in intersections
            ):
                continue
            if not _document_source_ids(document).issubset(
                {source["source_id"] for source in candidate_sources}
            ):
                continue
            qualifying_documents.append(document)

        gaps: List[Dict[str, str]] = []
        if not candidate_sources:
            gaps.append(_gap("NO_CANDIDATE_SOURCE", "no usable classified source covers the requirement"))
        if len(candidate_sources) < requirement["minimum_independent_sources"]:
            gaps.append(
                _gap(
                    "INSUFFICIENT_INDEPENDENT_SOURCES",
                    "found {0} candidate sources; require {1}".format(
                        len(candidate_sources), requirement["minimum_independent_sources"]
                    ),
                )
            )
        if len(qualifying_documents) < requirement["minimum_approved_documents"]:
            gaps.append(
                _gap(
                    "INSUFFICIENT_APPROVED_DOCUMENTS",
                    "found {0} approved documents; require {1}".format(
                        len(qualifying_documents), requirement["minimum_approved_documents"]
                    ),
                )
            )
        qualifying_document_ids = {item["document_id"] for item in qualifying_documents}
        for document_id in sorted(
            set(requirement["selector"]["required_document_ids"])
            - qualifying_document_ids
        ):
            gaps.append(
                _gap("MISSING_REQUIRED_DOCUMENT", "required document is not ready: " + document_id)
            )
        qualifying_fact_ids = {
            fact["fact_id"]
            for document in qualifying_documents
            for fact in document["structured_facts"]
        }
        for fact_id in sorted(
            set(requirement["selector"]["required_fact_ids"]) - qualifying_fact_ids
        ):
            gaps.append(_gap("MISSING_REQUIRED_FACT", "required fact is not ready: " + fact_id))

        if conflicts:
            status = "CONFLICTED"
            actions = [
                _action(
                    "HUMAN_CONFLICT_REVIEW",
                    "resolve classified evidence conflicts before approval or publication",
                )
            ]
        elif gaps:
            status = "GAP"
            if candidate_sources and not qualifying_documents:
                actions = [
                    _action(
                        "ACQUIRE_AND_CLASSIFY",
                        "acquire from candidate sources, then classify and review canonical documents",
                    )
                ]
            elif not candidate_sources:
                actions = [
                    _action(
                        "HUMAN_SOURCE_REVIEW",
                        "identify and authorize a source matching mission scope and rights policy",
                    )
                ]
            else:
                actions = [
                    _action(
                        "COMPLETE_DOCUMENT_REVIEW",
                        "close required-document, fact, scope, or approval gaps",
                    )
                ]
        else:
            status = "COVERED"
            actions = []

        requirement_reports.append(
            {
                "requirement_id": requirement_id,
                "safety_class": requirement["safety_class"],
                "status": status,
                "candidate_source_ids": sorted(source["source_id"] for source in candidate_sources),
                "classified_document_ids": sorted(
                    document["document_id"] for document in related_documents
                ),
                "qualifying_document_ids": sorted(qualifying_document_ids),
                "gaps": sorted(gaps, key=lambda item: (item["code"], item["message"])),
                "conflicts": conflicts,
                "recommended_actions": actions,
            }
        )

    statuses = [item["status"] for item in requirement_reports]
    decision = "CONFLICTS" if "CONFLICTED" in statuses else ("GAPS" if "GAP" in statuses else "COVERED")
    report: Dict[str, Any] = {
        "schema_version": REPORT_VERSION,
        "record_type": "coverage_plan",
        "event_id": policy["event_id"],
        "policy_id": policy["policy_id"],
        "registry_id": registry["registry_id"],
        "as_of": as_of,
        "decision": decision,
        "summary": {
            "requirements": len(requirement_reports),
            "covered": statuses.count("COVERED"),
            "gaps": statuses.count("GAP"),
            "conflicted": statuses.count("CONFLICTED"),
        },
        "requirements": requirement_reports,
        "source_findings": sorted(
            all_source_findings,
            key=lambda item: (item["source_id"], item["code"], item["message"]),
        ),
        "conflicts": sorted(
            all_conflicts,
            key=lambda item: (
                item["requirement_id"],
                item["kind"],
                item.get("fact_id", ""),
            ),
        ),
        "automation_boundary": {
            "advisory_only": True,
            "network_access": False,
            "publishes_release": False,
            "human_approval_required": True,
        },
    }
    report["plan_id"] = _digest(report)
    return report


def plan_mission_coverage(
    mission: Mapping[str, Any],
    registry: Mapping[str, Any],
    classifications: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    documents: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Plan coverage for the mission-profile/document-classification contracts.

    Canonical documents are optional because sourcing starts before acquisition.
    When supplied, declared and fact-value conflicts are included and block the
    affected requirement.  The planner never resolves those conflicts itself.
    """

    if not isinstance(mission, Mapping):
        raise CoveragePlanError("mission must be an object")
    if not isinstance(registry, Mapping):
        raise CoveragePlanError("registry must be an object")
    if isinstance(classifications, (str, bytes)) or not isinstance(classifications, Sequence):
        raise CoveragePlanError("classifications must be a sequence")
    if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
        raise CoveragePlanError("documents must be a sequence")
    planning_time = _parse_time(as_of)
    _validate(mission, "corpus_mission_profile", "corpus mission profile")
    _validate(registry, "source_registry", "source registry")
    if mission["event"]["event_id"] != registry["event_id"]:
        raise CoveragePlanError("mission and source registry event_id values do not match")

    source_index = {source["source_id"]: source for source in registry["sources"]}
    usable_sources: Set[str] = set()
    source_findings: List[Dict[str, str]] = []
    for source_id, source in sorted(source_index.items()):
        reasons = []
        if not source["enabled"]:
            reasons.append(("SOURCE_DISABLED", "source is disabled"))
        if source.get("approval", {}).get("status") != "approved":
            reasons.append(("SOURCE_NOT_APPROVED", "source has not received human approval"))
        if source["rights"]["redistribution"] == "prohibited":
            reasons.append(("REDISTRIBUTION_PROHIBITED", "source cannot be redistributed"))
        if not _active_at(source["freshness"], planning_time, "effective_from"):
            reasons.append(("SOURCE_OUTSIDE_VALIDITY", "source is not valid at planning time"))
        if reasons:
            source_findings.extend(
                {"source_id": source_id, "code": code, "message": message}
                for code, message in reasons
            )
        else:
            usable_sources.add(source_id)

    classification_ids: Set[str] = set()
    valid_classifications: List[Mapping[str, Any]] = []
    for index, classification in enumerate(classifications):
        if not isinstance(classification, Mapping):
            raise CoveragePlanError("classification {0} must be an object".format(index))
        _validate(classification, "document_classification", "classification {0}".format(index))
        if classification["mission_profile_id"] != mission["mission_profile_id"]:
            raise CoveragePlanError("classification {0} names a different mission".format(index))
        if classification["classification_id"] in classification_ids:
            raise CoveragePlanError("duplicate classification_id: " + classification["classification_id"])
        classification_ids.add(classification["classification_id"])
        valid_classifications.append(classification)

    canonical_by_id: Dict[str, List[Mapping[str, Any]]] = {}
    for index, document in enumerate(documents):
        if not isinstance(document, Mapping):
            raise CoveragePlanError("document {0} must be an object".format(index))
        _validate(document, "canonical_document", "document {0}".format(index))
        if document["event_id"] != mission["event"]["event_id"]:
            raise CoveragePlanError("document {0} event_id does not match mission".format(index))
        canonical_by_id.setdefault(document["document_id"], []).append(document)

    reports: List[Dict[str, Any]] = []
    all_conflicts: List[Dict[str, Any]] = []
    mission_scope = mission["scope"]
    for requirement in sorted(mission["required_information"], key=lambda item: item["requirement_id"]):
        related = [
            item
            for item in valid_classifications
            if requirement["category_id"] in item["coverage"]["category_ids"]
        ]
        related_documents = [
            document
            for item in related
            for document in canonical_by_id.get(item["document"]["document_id"], [])
        ]
        conflicts = _find_conflicts(related_documents)
        all_conflicts.extend(
            {"requirement_id": requirement["requirement_id"], **conflict}
            for conflict in conflicts
        )

        qualifying = []
        for item in related:
            provenance = item["provenance"]
            coverage = item["coverage"]
            if item["verification"]["status"] != "verified":
                continue
            if provenance["redistribution"] in {"prohibited", "unknown"}:
                continue
            if not _active_at(item["validity"], planning_time, "valid_from"):
                continue
            if SAFETY_RANK[item["risk"]["safety_class"]] < SAFETY_RANK[requirement["safety_class"]]:
                continue
            if not set(mission_scope["geographies"]).issubset(coverage["geographies"]):
                continue
            if not set(mission_scope["languages"]).issubset(coverage["languages"]):
                continue
            if not set(requirement["required_intents"]).issubset(coverage["supported_intents"]):
                continue
            if not set(provenance["source_ids"]).issubset(usable_sources):
                continue
            qualifying.append(item)

        qualifying_sources = sorted(
            {
                source_id
                for item in qualifying
                for source_id in item["provenance"]["source_ids"]
            }
        )
        gaps: List[Dict[str, str]] = []
        covered_intents = {
            intent for item in related for intent in item["coverage"]["supported_intents"]
        }
        for intent in sorted(set(requirement["required_intents"]) - covered_intents):
            gaps.append(_gap("MISSING_INTENT", "no classification supports intent: " + intent))
        if len(qualifying_sources) < requirement["minimum_sources"]:
            gaps.append(
                _gap(
                    "INSUFFICIENT_QUALIFYING_SOURCES",
                    "found {0} qualifying sources; require {1}".format(
                        len(qualifying_sources), requirement["minimum_sources"]
                    ),
                )
            )
        if not qualifying:
            gaps.append(
                _gap(
                    "NO_QUALIFYING_CLASSIFICATION",
                    "no verified, current, scoped classification covers every required intent",
                )
            )

        if conflicts:
            status = "CONFLICTED"
            actions = [_action("HUMAN_CONFLICT_REVIEW", "resolve evidence conflicts before promotion")]
        elif gaps:
            status = "GAP"
            actions = [
                _action(
                    "SOURCE_OR_CLASSIFICATION_WORK",
                    "source, acquire, classify, and review evidence for the listed gaps",
                )
            ]
        else:
            status = "COVERED"
            actions = []
        reports.append(
            {
                "requirement_id": requirement["requirement_id"],
                "category_id": requirement["category_id"],
                "safety_class": requirement["safety_class"],
                "status": status,
                "classification_ids": sorted(item["classification_id"] for item in related),
                "qualifying_classification_ids": sorted(item["classification_id"] for item in qualifying),
                "qualifying_source_ids": qualifying_sources,
                "gaps": sorted(gaps, key=lambda item: (item["code"], item["message"])),
                "conflicts": conflicts,
                "recommended_actions": actions,
            }
        )

    statuses = [item["status"] for item in reports]
    decision = "CONFLICTS" if "CONFLICTED" in statuses else ("GAPS" if "GAP" in statuses else "COVERED")
    report: Dict[str, Any] = {
        "schema_version": REPORT_VERSION,
        "record_type": "coverage_report",
        "mission_profile_id": mission["mission_profile_id"],
        "event_id": mission["event"]["event_id"],
        "registry_id": registry["registry_id"],
        "as_of": as_of,
        "decision": decision,
        "summary": {
            "requirements": len(reports),
            "covered": statuses.count("COVERED"),
            "gaps": statuses.count("GAP"),
            "conflicted": statuses.count("CONFLICTED"),
        },
        "requirements": reports,
        "source_findings": sorted(source_findings, key=lambda item: (item["source_id"], item["code"])),
        "conflicts": sorted(all_conflicts, key=lambda item: (item["requirement_id"], item["kind"])),
        "automation_boundary": {
            "advisory_only": True,
            "network_access": False,
            "publishes_release": False,
            "human_approval_required": True,
        },
    }
    report["report_id"] = _digest(report)
    return report
