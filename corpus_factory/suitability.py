"""Operational corpus suitability evaluation for a declared event policy.

The evaluator is intentionally independent of retrieval scores.  It proves that
the source and canonical-document inventory is complete, current, authorized,
traceable, and covered by approved evaluation cases before release construction.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from corpus_factory.validator import (
    ContractValidationError,
    event_policy_subject_digest,
    validate_instance,
)


REPORT_VERSION = "1.0.0"
CASE_FIELDS = {
    "case_id",
    "requirement_id",
    "family",
    "geography",
    "language",
    "audience",
    "channel",
    "approved",
}
CASE_FAMILIES = ("positive", "boundary", "no_answer")
TIME_CONFIDENCE_VALUES = ("trusted", "anchored", "untrusted", "unknown")
SAFETY_RANK = {"advisory": 0, "standard": 1, "high": 2, "critical": 3}


class SuitabilityInputError(ValueError):
    """Raised only when the evaluator cannot form a deterministic JSON report."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SuitabilityInputError(f"input is not canonical JSON: {exc}") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _aggregate_digest(records: Sequence[Mapping[str, Any]], key: str) -> str:
    ordered = sorted(
        records,
        key=lambda record: (
            str(record.get(key, "")) if isinstance(record, Mapping) else "",
            str(record.get("revision", "")) if isinstance(record, Mapping) else "",
            canonical_json(record),
        ),
    )
    return _digest(ordered)


def _failure(
    code: str, path: str, message: str, requirement_id: Optional[str] = None
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"code": code, "path": path, "message": message}
    if requirement_id is not None:
        result["requirement_id"] = requirement_id
    return result


def _parse_time(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO-8601 time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{path} must include a timezone")
    return parsed


def _authority_entry(
    registry: Sequence[Mapping[str, Any]], source: Mapping[str, Any]
) -> Optional[Mapping[str, Any]]:
    """Resolve the single registry entry authorizing a source and publisher."""

    source_id = source.get("source_id")
    publisher = source.get("publisher")
    publisher_id = publisher.get("id") if isinstance(publisher, Mapping) else None
    matches = [
        entry
        for entry in registry
        if source_id in entry.get("source_ids", [])
        and publisher_id in entry.get("publisher_ids", [])
        and source.get("authority_class") == entry.get("authority_class")
    ]
    return matches[0] if len(matches) == 1 else None


def _validate_policy(
    policy: Mapping[str, Any], failures: List[Dict[str, Any]]
) -> Dict[str, Mapping[str, Any]]:
    try:
        validate_instance(policy, "event_policy")
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        failures.append(_failure("INVALID_EVENT_POLICY", "policy", str(exc)))
    result: Dict[str, Mapping[str, Any]] = {}
    requirements = policy.get("coverage_requirements")
    requirements = requirements if isinstance(requirements, list) else []
    for index, raw in enumerate(requirements):
        path = f"policy.coverage_requirements[{index}]"
        if not isinstance(raw, Mapping):
            failures.append(_failure("MALFORMED_REQUIREMENT", path, "requirement must be an object"))
            continue
        requirement_id = raw.get("requirement_id")
        if not isinstance(requirement_id, str) or not requirement_id:
            failures.append(_failure("MALFORMED_REQUIREMENT", path + ".requirement_id", "requirement_id is required"))
            continue
        if requirement_id in result:
            failures.append(_failure("DUPLICATE_REQUIREMENT", path, f"duplicate requirement_id: {requirement_id}"))
            continue
        result[requirement_id] = raw
    return result


def _index_sources(
    sources: Sequence[Mapping[str, Any]],
    registry: Sequence[Mapping[str, Any]],
    failures: List[Dict[str, Any]],
) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    seen: Set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            failures.append(
                _failure("MALFORMED_SOURCE", f"sources[{index}]", "source must be an object")
            )
            continue
        try:
            validate_instance(source, "source_record")
        except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
            failures.append(
                _failure("MALFORMED_SOURCE", f"sources[{index}]", str(exc))
            )
            continue
        source_id = source.get("source_id") if isinstance(source, Mapping) else None
        if not isinstance(source_id, str) or not source_id:
            failures.append(_failure("MALFORMED_SOURCE", f"sources[{index}]", "source_id is required"))
            continue
        if source_id in seen:
            failures.append(_failure("DUPLICATE_SOURCE", f"sources[{index}]", f"duplicate source_id: {source_id}"))
            continue
        seen.add(source_id)
        entry = _authority_entry(registry, source)
        if entry is None:
            failures.append(_failure("UNREGISTERED_SOURCE", f"sources[{index}]", f"source, publisher, and authority class are not jointly authorized: {source_id}"))
            continue
        source_scope = source.get("scope")
        if not isinstance(source_scope, Mapping):
            failures.append(_failure("MALFORMED_SOURCE", f"sources[{index}].scope", "source scope is required"))
            continue
        scope_valid = True
        for field in ("subjects", "geographies"):
            source_values = source_scope.get(field)
            if not isinstance(source_values, list) or not set(source_values).issubset(set(entry.get(field, []))):
                failures.append(_failure("AUTHORITY_SCOPE_MISMATCH", f"sources[{index}].scope.{field}", f"source {field} exceed its authority registration"))
                scope_valid = False
        if scope_valid:
            result[source_id] = source
    return result


def _citation_lineage_errors(
    document: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    path: str,
) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
    citations: List[Tuple[str, Any]] = []
    source_citations = document.get("source_citations")
    if not isinstance(source_citations, list) or not source_citations:
        failures.append(_failure("MALFORMED_DOCUMENT", path + ".source_citations", "source citations are required"))
    else:
        citations.extend((path + f".source_citations[{index}]", value) for index, value in enumerate(source_citations))
    facts = document.get("structured_facts")
    if not isinstance(facts, list):
        failures.append(_failure("MALFORMED_DOCUMENT", path + ".structured_facts", "structured facts must be a list"))
    else:
        for fact_index, fact in enumerate(facts):
            fact_citations = fact.get("citations") if isinstance(fact, Mapping) else None
            if not isinstance(fact_citations, list) or not fact_citations:
                failures.append(_failure("MALFORMED_DOCUMENT", path + f".structured_facts[{fact_index}]", "fact citations are required"))
                continue
            citations.extend((path + f".structured_facts[{fact_index}].citations[{index}]", value) for index, value in enumerate(fact_citations))
    for citation_path, citation in citations:
        if not isinstance(citation, Mapping):
            failures.append(_failure("MALFORMED_CITATION", citation_path, "citation must be an object"))
            continue
        source_id = citation.get("source_id")
        source = sources.get(source_id) if isinstance(source_id, str) else None
        if source is None:
            failures.append(_failure("UNKNOWN_SOURCE_REFERENCE", citation_path, f"unknown source reference: {source_id}"))
            continue
        evidence = source.get("evidence")
        expected_digest = evidence.get("digest") if isinstance(evidence, Mapping) else None
        if citation.get("evidence_digest") != expected_digest:
            failures.append(_failure("BROKEN_EVIDENCE_LINEAGE", citation_path + ".evidence_digest", "citation digest does not match registered source evidence"))
    return failures


def _within_window(record: Mapping[str, Any], as_of: datetime, source: bool) -> bool:
    window = record.get("freshness" if source else "validity")
    if not isinstance(window, Mapping):
        return False
    start_name = "effective_from" if source else "valid_from"
    try:
        start = _parse_time(window.get(start_name), start_name)
        end = _parse_time(window.get("valid_until"), "valid_until") if window.get("valid_until") is not None else None
        review_due = None if source else _parse_time(window.get("review_due_at"), "review_due_at")
    except (TypeError, ValueError):
        return False
    return as_of >= start and (end is None or as_of < end) and (review_due is None or as_of <= review_due)


def _within_maximum_age(
    source: Mapping[str, Any], as_of: datetime, maximum_age_seconds: Any
) -> bool:
    if not isinstance(maximum_age_seconds, int) or isinstance(maximum_age_seconds, bool):
        return False
    try:
        last_verified = _parse_time(source.get("last_verified_at"), "last_verified_at")
    except (TypeError, ValueError):
        return False
    age_seconds = (as_of - last_verified).total_seconds()
    return 0 <= age_seconds <= maximum_age_seconds


def _document_source_ids(document: Mapping[str, Any]) -> Set[str]:
    result: Set[str] = set()
    for citation in document.get("source_citations", []):
        if isinstance(citation, Mapping) and isinstance(citation.get("source_id"), str):
            result.add(citation["source_id"])
    return result


def _document_fact_ids(document: Mapping[str, Any]) -> Set[str]:
    return {
        fact["fact_id"]
        for fact in document.get("structured_facts", [])
        if isinstance(fact, Mapping) and isinstance(fact.get("fact_id"), str)
    }


def _intersection_values(requirement: Mapping[str, Any]) -> Iterable[Tuple[str, str, str, str]]:
    scope = requirement.get("scope")
    scope = scope if isinstance(scope, Mapping) else {}
    return itertools.product(
        scope.get("geographies", []),
        scope.get("languages", []),
        scope.get("audiences", []),
        scope.get("delivery_channels", []),
    )


def _document_covers(document: Mapping[str, Any], intersection: Tuple[str, str, str, str]) -> bool:
    geography, language, audience, channel = intersection
    scope = document.get("scope")
    return (
        isinstance(scope, Mapping)
        and geography in scope.get("geographies", [])
        and language in scope.get("languages", [])
        and audience in scope.get("audiences", [])
        and channel in document.get("permitted_delivery_channels", [])
    )


def _evaluate_requirement(
    requirement_id: str,
    requirement: Mapping[str, Any],
    event_id: Any,
    sources: Mapping[str, Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    as_of: datetime,
    time_confidence: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    failures: List[Dict[str, Any]] = []
    safety_class = requirement.get("safety_class")
    if safety_class == "critical" and time_confidence != "trusted":
        failures.append(_failure("UNTRUSTED_TIME", f"requirements.{requirement_id}", "critical suitability requires trusted time", requirement_id))

    candidates: List[Mapping[str, Any]] = []
    allowed_authorities = set(requirement.get("allowed_authority_classes", []))
    selector = requirement.get("selector")
    selector = selector if isinstance(selector, Mapping) else {}
    required_documents = set(selector.get("required_document_ids", []))
    required_facts = set(selector.get("required_fact_ids", []))
    freshness = requirement.get("freshness")
    freshness = freshness if isinstance(freshness, Mapping) else {}
    stale_source_ids: Set[str] = set()
    for document in documents:
        if document.get("event_id") != event_id:
            continue
        document_class = document.get("safety_class")
        if document_class not in SAFETY_RANK or safety_class not in SAFETY_RANK:
            continue
        if SAFETY_RANK[document_class] < SAFETY_RANK[safety_class]:
            continue
        document_facts = _document_fact_ids(document)
        if (
            document.get("document_id") not in required_documents
            and not document_facts.intersection(required_facts)
        ):
            continue
        if document.get("approval_state") != "approved" or document.get("conflict", {}).get("state") == "contested":
            continue
        if not _within_window(document, as_of, source=False):
            continue
        source_ids = _document_source_ids(document)
        if not source_ids:
            continue
        if any(source_id not in sources for source_id in source_ids):
            continue
        if any(sources[source_id].get("authority_class") not in allowed_authorities for source_id in source_ids):
            continue
        if any(not _within_window(sources[source_id], as_of, source=True) for source_id in source_ids):
            continue
        too_old = {
            source_id
            for source_id in source_ids
            if not _within_maximum_age(
                sources[source_id], as_of, freshness.get("maximum_age_seconds")
            )
        }
        if too_old:
            stale_source_ids.update(too_old)
            continue
        candidates.append(document)

    if not candidates:
        failures.append(_failure("NO_QUALIFYING_DOCUMENT", f"requirements.{requirement_id}", "no approved, uncontested, current, authorized document satisfies the requirement", requirement_id))

    for source_id in sorted(stale_source_ids):
        failures.append(
            _failure(
                "SOURCE_TOO_OLD",
                f"requirements.{requirement_id}.freshness.maximum_age_seconds",
                f"source exceeds maximum verification age: {source_id}",
                requirement_id,
            )
        )

    minimum_documents = requirement.get("minimum_approved_documents")
    if isinstance(minimum_documents, int) and len(candidates) < minimum_documents:
        failures.append(
            _failure(
                "INSUFFICIENT_APPROVED_DOCUMENTS",
                f"requirements.{requirement_id}.minimum_approved_documents",
                f"found {len(candidates)}, require {minimum_documents}",
                requirement_id,
            )
        )

    candidate_document_ids = {document.get("document_id") for document in candidates}
    for document_id in sorted(required_documents - candidate_document_ids):
        failures.append(_failure("MISSING_REQUIRED_DOCUMENT", f"requirements.{requirement_id}.required_document_ids", f"required document is not suitable: {document_id}", requirement_id))

    candidate_facts: Set[str] = set()
    for document in candidates:
        candidate_facts.update(_document_fact_ids(document))
    for fact_id in sorted(required_facts - candidate_facts):
        failures.append(_failure("MISSING_REQUIRED_FACT", f"requirements.{requirement_id}.required_fact_ids", f"required fact is absent: {fact_id}", requirement_id))

    independent_sources: Set[str] = set()
    for document in candidates:
        independent_sources.update(_document_source_ids(document))
    minimum_sources = requirement.get("minimum_independent_sources")
    if isinstance(minimum_sources, int) and len(independent_sources) < minimum_sources:
        failures.append(_failure("INSUFFICIENT_INDEPENDENT_SOURCES", f"requirements.{requirement_id}.minimum_independent_sources", f"found {len(independent_sources)}, require {minimum_sources}", requirement_id))

    intersections = []
    for intersection in _intersection_values(requirement):
        key = {"geography": intersection[0], "language": intersection[1], "audience": intersection[2], "channel": intersection[3]}
        covered_documents = sorted(
            str(document.get("document_id"))
            for document in candidates
            if _document_covers(document, intersection)
        )
        if not covered_documents:
            failures.append(_failure("SCOPE_COVERAGE_GAP", f"requirements.{requirement_id}.scope", "no suitable document covers " + canonical_json(key), requirement_id))
        counts = {}
        evaluation_minima = requirement.get("evaluation_cases")
        evaluation_minima = evaluation_minima if isinstance(evaluation_minima, Mapping) else {}
        minimum_fields = {
            "positive": "minimum_positive",
            "boundary": "minimum_boundary",
            "no_answer": "minimum_no_answer",
        }
        for family in CASE_FAMILIES:
            count = sum(
                1
                for case in cases
                if case.get("requirement_id") == requirement_id
                and case.get("approved") is True
                and case.get("family") == family
                and case.get("geography") == intersection[0]
                and case.get("language") == intersection[1]
                and case.get("audience") == intersection[2]
                and case.get("channel") == intersection[3]
            )
            counts[family] = count
            minimum = evaluation_minima.get(minimum_fields[family], 0)
            if count < minimum:
                failures.append(_failure("EVALUATION_COVERAGE_GAP", f"requirements.{requirement_id}.cases.{family}", f"{canonical_json(key)} has {count}, requires {minimum}", requirement_id))
        intersections.append({**key, "document_ids": covered_documents, "case_counts": counts})

    return (
        {
            "requirement_id": requirement_id,
            "safety_class": safety_class,
            "qualifying_document_ids": sorted(str(document.get("document_id")) for document in candidates),
            "independent_source_ids": sorted(independent_sources),
            "intersections": sorted(intersections, key=lambda item: (item["geography"], item["language"], item["audience"], item["channel"])),
            "passed": not failures,
        },
        failures,
    )


def evaluate_suitability(
    policy: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    evaluation_cases: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    time_confidence: str,
) -> Dict[str, Any]:
    """Return a deterministic PASS/FAIL suitability report for one event."""

    if not isinstance(policy, Mapping):
        raise SuitabilityInputError("policy must be an object")
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise SuitabilityInputError("sources must be a sequence")
    if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
        raise SuitabilityInputError("documents must be a sequence")
    if isinstance(evaluation_cases, (str, bytes)) or not isinstance(evaluation_cases, Sequence):
        raise SuitabilityInputError("evaluation_cases must be a sequence")

    failures: List[Dict[str, Any]] = []
    try:
        as_of_time = _parse_time(as_of, "as_of")
    except (TypeError, ValueError) as exc:
        failures.append(_failure("INVALID_TIME", "as_of", str(exc)))
        as_of_time = datetime.min.replace(tzinfo=timezone.utc)
    if time_confidence not in TIME_CONFIDENCE_VALUES:
        failures.append(_failure("INVALID_TIME_CONFIDENCE", "time_confidence", "unknown time confidence"))

    requirements = _validate_policy(policy, failures)
    registry = policy.get("authority_registry") if isinstance(policy.get("authority_registry"), list) else []
    source_index = _index_sources(sources, registry, failures)

    document_ids: Set[Tuple[Any, Any]] = set()
    valid_documents: List[Mapping[str, Any]] = []
    for index, document in enumerate(documents):
        path = f"documents[{index}]"
        if not isinstance(document, Mapping):
            failures.append(_failure("MALFORMED_DOCUMENT", path, "document must be an object"))
            continue
        try:
            validate_instance(document, "canonical_document")
        except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
            failures.append(_failure("MALFORMED_DOCUMENT", path, str(exc)))
            continue
        identity = (document.get("document_id"), document.get("revision"))
        if not isinstance(identity[0], str) or not isinstance(identity[1], int):
            failures.append(_failure("MALFORMED_DOCUMENT", path, "document_id and revision are required"))
        elif identity in document_ids:
            failures.append(_failure("DUPLICATE_DOCUMENT", path, "duplicate document revision"))
        document_ids.add(identity)
        if document.get("event_id") != policy.get("event_id"):
            failures.append(_failure("WRONG_EVENT_DOCUMENT", path + ".event_id", "document event does not match policy"))
            continue
        lineage_failures = _citation_lineage_errors(document, source_index, path)
        failures.extend(lineage_failures)
        if not lineage_failures:
            valid_documents.append(document)

    case_ids: Set[str] = set()
    valid_cases: List[Mapping[str, Any]] = []
    for index, case in enumerate(evaluation_cases):
        path = f"evaluation_cases[{index}]"
        case_failure_count = len(failures)
        if not isinstance(case, Mapping) or set(case) != CASE_FIELDS:
            failures.append(_failure("MALFORMED_CASE", path, "case fields do not match the inventory contract"))
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            failures.append(_failure("MALFORMED_CASE", path + ".case_id", "case_id is required"))
        elif case_id in case_ids:
            failures.append(_failure("DUPLICATE_CASE", path, f"duplicate case_id: {case_id}"))
        else:
            case_ids.add(case_id)
        if case.get("requirement_id") not in requirements:
            failures.append(_failure("UNKNOWN_REQUIREMENT_REFERENCE", path + ".requirement_id", "case references an unknown requirement"))
        else:
            requirement = requirements[case["requirement_id"]]
            field_pairs = (
                ("geography", "geographies"),
                ("language", "languages"),
                ("audience", "audiences"),
                ("channel", "delivery_channels"),
            )
            requirement_scope = requirement.get("scope")
            requirement_scope = requirement_scope if isinstance(requirement_scope, Mapping) else {}
            for case_field, requirement_field in field_pairs:
                if case.get(case_field) not in requirement_scope.get(requirement_field, []):
                    failures.append(
                        _failure(
                            "CASE_SCOPE_MISMATCH",
                            path + "." + case_field,
                            "case value is outside its referenced requirement",
                            case["requirement_id"],
                        )
                    )
        if case.get("family") not in CASE_FAMILIES:
            failures.append(_failure("MALFORMED_CASE", path + ".family", "unknown case family"))
        if not isinstance(case.get("approved"), bool):
            failures.append(_failure("MALFORMED_CASE", path + ".approved", "approved must be boolean"))
        if len(failures) == case_failure_count:
            valid_cases.append(case)

    requirement_results = []
    for requirement_id in sorted(requirements):
        result, requirement_failures = _evaluate_requirement(
            requirement_id,
            requirements[requirement_id],
            policy.get("event_id"),
            source_index,
            valid_documents,
            valid_cases,
            as_of_time,
            time_confidence,
        )
        requirement_results.append(result)
        failures.extend(requirement_failures)

    failures.sort(key=lambda item: (item.get("requirement_id", ""), item["path"], item["code"], item["message"]))
    bindings = {
        "event_policy_subject_digest": event_policy_subject_digest(policy),
        "event_policy_record_digest": _digest(policy),
        "source_aggregate_digest": _aggregate_digest(sources, "source_id"),
        "document_aggregate_digest": _aggregate_digest(documents, "document_id"),
        "case_aggregate_digest": _aggregate_digest(evaluation_cases, "case_id"),
    }
    report_body = {
        "schema_version": REPORT_VERSION,
        "event_id": policy.get("event_id"),
        "as_of": as_of,
        "time_confidence": time_confidence,
        "bindings": bindings,
        "requirements": requirement_results,
        "failures": failures,
        "decision": "PASS" if not failures and all(item["passed"] for item in requirement_results) else "FAIL",
    }
    return {"report_id": _digest(report_body), **report_body}
