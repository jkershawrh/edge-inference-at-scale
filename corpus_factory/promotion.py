"""Deterministic, fail-closed corpus release promotion gate.

The gate consumes summaries produced by five independent evaluators.  It does
not average their results: every layer and every safety class must pass its own
contract.  All evidence is also bound to one immutable release/runtime tuple.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from corpus_factory.validator import ContractValidationError, validate_instance
from corpus_factory.governance import (
    CONTRACT_PROFILES,
    GOVERNED_PROFILE,
    LEGACY_PROFILE,
    object_digest,
)


SCHEMA_VERSION = "1.1.0"
SPEC_VERSION = "1.0"

BINDING_FIELDS = (
    "release_digest",
    "corpus_digest",
    "eval_set_digest",
    "model_digest",
    "embedding_model_digest",
    "chunker_digest",
    "policy_digest",
    "source_aggregate_digest",
    "document_aggregate_digest",
    "case_aggregate_digest",
)

SAFETY_CLASSES = ("critical", "high", "standard", "advisory")

COVERAGE_IDENTITY_FIELDS = (
    "schema_version",
    "report_id",
    "mission_profile_id",
    "event_id",
    "registry_id",
)

RETRIEVAL_THRESHOLDS = {
    "critical": {
        "recall_at_3": ("min", 1.0),
        "required_evidence_coverage_at_5": ("min", 1.0),
        "mrr": ("min", 0.95),
        "scope_accuracy": ("min", 1.0),
        "forbidden_context_rate": ("max", 0.0),
    },
    "high": {
        "recall_at_3": ("min", 0.99),
        "required_evidence_coverage_at_5": ("min", 0.99),
        "mrr": ("min", 0.90),
        "scope_accuracy": ("min", 1.0),
        "forbidden_context_rate": ("max", 0.0),
    },
    "standard": {
        "recall_at_3": ("min", 0.97),
        "required_evidence_coverage_at_5": ("min", 0.98),
        "mrr": ("min", 0.85),
        "scope_accuracy": ("min", 0.995),
        "forbidden_context_rate": ("max", 0.0),
    },
    "advisory": {
        "recall_at_3": ("min", 0.95),
        "required_evidence_coverage_at_5": ("min", 0.95),
        "mrr": ("min", 0.80),
        "scope_accuracy": ("min", 0.99),
        "forbidden_context_rate": ("max", 0.0),
    },
}

ANSWER_THRESHOLDS = {
    "critical": {
        "grounded_pass_rate": ("min", 1.0),
        "claim_precision": ("min", 1.0),
        "citation_precision": ("min", 1.0),
        "citation_recall": ("min", 1.0),
        "no_answer_recall": ("min", 1.0),
        "stale_refusal_recall": ("min", 1.0),
        "critical_entity_preservation": ("min", 1.0),
        "unsupported_claim_rate": ("max", 0.0),
    },
    "high": {
        "grounded_pass_rate": ("min", 0.99),
        "claim_precision": ("min", 0.995),
        "citation_precision": ("min", 0.995),
        "citation_recall": ("min", 0.99),
        "no_answer_recall": ("min", 0.995),
        "stale_refusal_recall": ("min", 1.0),
        "unsupported_claim_rate": ("max", 0.005),
    },
    "standard": {
        "grounded_pass_rate": ("min", 0.97),
        "claim_precision": ("min", 0.99),
        "citation_precision": ("min", 0.99),
        "citation_recall": ("min", 0.98),
        "no_answer_recall": ("min", 0.98),
        "stale_refusal_recall": ("min", 1.0),
        "unsupported_claim_rate": ("max", 0.01),
    },
    "advisory": {
        "grounded_pass_rate": ("min", 0.95),
        "claim_precision": ("min", 0.98),
        "citation_precision": ("min", 0.98),
        "citation_recall": ("min", 0.95),
        "no_answer_recall": ("min", 0.97),
        "stale_refusal_recall": ("min", 1.0),
        "unsupported_claim_rate": ("max", 0.02),
    },
}

RELEASE_CHECKS = (
    "schema",
    "lineage",
    "scope",
    "freshness",
    "conflicts",
    "licenses",
    "approvals",
    "hashes",
    "signatures",
    "artifact_completeness",
)

EDGE_THRESHOLDS = {
    "oom_kills": ("max", 0.0),
    "evictions": ("max", 0.0),
    "corrupt_indexes": ("max", 0.0),
    "failed_readiness_transitions": ("max", 0.0),
    "storage_headroom_fraction": ("min", 0.20),
    "memory_headroom_fraction": ("min", 0.15),
    "retrieval_error_rate": ("max", 0.0),
    "grounded_answer_error_rate": ("lt", 0.005),
    "warm_retrieval_p95_ms": ("max", 750.0),
    "warm_retrieval_p99_ms": ("max", 1500.0),
    "critical_rag_direct_p95_ms": ("max", 2000.0),
}

_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used for report identity."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _failure(layer: str, code: str, path: str, message: str) -> Dict[str, str]:
    return {"layer": layer, "code": code, "path": path, "message": message}


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _threshold_passes(value: float, operation: str, threshold: float) -> bool:
    if operation == "min":
        return value >= threshold
    if operation == "max":
        return value <= threshold
    return value < threshold


def _check_thresholds(
    layer: str,
    base_path: str,
    values: Mapping[str, Any],
    thresholds: Mapping[str, Tuple[str, float]],
    failures: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    for metric in sorted(thresholds):
        operation, threshold = thresholds[metric]
        value = values.get(metric)
        passed = _number(value) and _threshold_passes(float(value), operation, threshold)
        checks.append(
            {
                "metric": metric,
                "operation": operation,
                "threshold": threshold,
                "value": value,
                "passed": passed,
            }
        )
        if not _number(value):
            failures.append(
                _failure(layer, "MISSING_METRIC", f"{base_path}.{metric}", "required numeric metric is missing")
            )
        elif not passed:
            failures.append(
                _failure(
                    layer,
                    "THRESHOLD_FAILED",
                    f"{base_path}.{metric}",
                    f"value {value} does not satisfy {operation} {threshold}",
                )
            )
    return checks


def _validate_bindings(
    evidence: Mapping[str, Mapping[str, Any]], failures: List[Dict[str, str]]
) -> Dict[str, Any]:
    resolved: Dict[str, Any] = {}
    for field in BINDING_FIELDS:
        observed: List[Tuple[str, Any]] = []
        for source_name, source in evidence.items():
            binding = source.get("binding")
            value = binding.get(field) if isinstance(binding, Mapping) else None
            observed.append((source_name, value))
            if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
                failures.append(
                    _failure(
                        "artifact_binding",
                        "MISSING_OR_INVALID_BINDING",
                        f"{source_name}.binding.{field}",
                        "required sha256 digest is missing or invalid",
                    )
                )
        valid_values = sorted({value for _, value in observed if isinstance(value, str) and _DIGEST_RE.fullmatch(value)})
        resolved[field] = valid_values[0] if len(valid_values) == 1 else None
        if len(valid_values) > 1:
            failures.append(
                _failure(
                    "artifact_binding",
                    "BINDING_MISMATCH",
                    f"binding.{field}",
                    "evidence names different immutable digests",
                )
            )
    return resolved


def _release_layer(source: Mapping[str, Any], failures: List[Dict[str, str]]) -> Dict[str, Any]:
    checks = source.get("checks")
    checks = checks if isinstance(checks, Mapping) else {}
    results = []
    for name in RELEASE_CHECKS:
        passed = checks.get(name) is True
        results.append({"check": name, "passed": passed})
        if not passed:
            failures.append(_failure("release_validity", "RELEASE_CHECK_FAILED", f"release_validity.checks.{name}", "required release check did not pass"))

    hard_counts = {
        "unresolved_critical_conflicts": source.get("unresolved_critical_conflicts"),
        "unapproved_critical_documents": source.get("unapproved_critical_documents"),
        "policy_violations": source.get("policy_violations"),
        "security_fixture_failures": source.get("security_fixture_failures"),
    }
    for name, value in hard_counts.items():
        if not _number(value):
            failures.append(_failure("release_validity", "MISSING_METRIC", f"release_validity.{name}", "required numeric hard-gate count is missing"))
        elif value != 0:
            failures.append(_failure("release_validity", "HARD_GATE_FAILED", f"release_validity.{name}", "hard-gate count must be zero"))
    if source.get("passed") is not True:
        failures.append(_failure("release_validity", "EVALUATOR_BLOCKED", "release_validity.passed", "release validity evaluator did not pass"))
    return {"checks": results, "hard_gate_counts": hard_counts}


def _suitability_layer(
    source: Mapping[str, Any],
    binding: Mapping[str, Any],
    failures: List[Dict[str, str]],
) -> Dict[str, Any]:
    report_id = source.get("report_id")
    if not isinstance(report_id, str) or not _DIGEST_RE.fullmatch(report_id):
        failures.append(
            _failure(
                "corpus_suitability",
                "INVALID_REPORT_ID",
                "suitability.report_id",
                "suitability report must have an immutable sha256 identity",
            )
        )
    source_failures = source.get("failures")
    if not isinstance(source_failures, list) or source_failures:
        failures.append(
            _failure(
                "corpus_suitability",
                "SUITABILITY_FAILURES",
                "suitability.failures",
                "suitability report contains failures or omits failure evidence",
            )
        )
    requirements = source.get("requirements")
    if (
        not isinstance(requirements, list)
        or not requirements
        or any(not isinstance(item, Mapping) or item.get("passed") is not True for item in requirements)
    ):
        failures.append(
            _failure(
                "corpus_suitability",
                "REQUIREMENT_BLOCKED",
                "suitability.requirements",
                "every declared corpus requirement must pass",
            )
        )
    if source.get("decision") != "PASS":
        failures.append(
            _failure(
                "corpus_suitability",
                "SUITABILITY_BLOCKED",
                "suitability.decision",
                "corpus suitability decision did not pass",
            )
        )

    suitability_bindings = source.get("bindings")
    suitability_bindings = suitability_bindings if isinstance(suitability_bindings, Mapping) else {}
    binding_map = {
        "policy_digest": "event_policy_subject_digest",
        "source_aggregate_digest": "source_aggregate_digest",
        "document_aggregate_digest": "document_aggregate_digest",
        "case_aggregate_digest": "case_aggregate_digest",
    }
    resolved = {}
    for promotion_name, suitability_name in binding_map.items():
        value = suitability_bindings.get(suitability_name)
        resolved[suitability_name] = value
        if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
            failures.append(
                _failure(
                    "corpus_suitability",
                    "MISSING_OR_INVALID_BINDING",
                    "suitability.bindings.{0}".format(suitability_name),
                    "suitability binding is missing or invalid",
                )
            )
        elif value != binding.get(promotion_name):
            failures.append(
                _failure(
                    "corpus_suitability",
                    "BINDING_MISMATCH",
                    "suitability.bindings.{0}".format(suitability_name),
                    "suitability evidence does not match the promoted artifact tuple",
                )
            )
    record_digest = suitability_bindings.get("event_policy_record_digest")
    resolved["event_policy_record_digest"] = record_digest
    if not isinstance(record_digest, str) or not _DIGEST_RE.fullmatch(record_digest):
        failures.append(
            _failure(
                "corpus_suitability",
                "MISSING_OR_INVALID_BINDING",
                "suitability.bindings.event_policy_record_digest",
                "full event policy record digest is missing or invalid",
            )
        )
    return {
        "report_id": report_id,
        "bindings": resolved,
        "requirement_count": len(requirements) if isinstance(requirements, list) else 0,
    }


def _coverage_layer(
    source: Mapping[str, Any],
    release_validity: Mapping[str, Any],
    suitability: Mapping[str, Any],
    failures: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Validate and bind an optional agentic coverage report.

    Coverage is advisory during sourcing, but once it is supplied to the
    promotion gate it becomes mandatory release evidence.  Its content-addressed
    identity must match the release evaluator's declared binding.
    """

    try:
        validate_instance(source, "coverage_report")
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        failures.append(
            _failure(
                "corpus_coverage",
                "INVALID_COVERAGE_REPORT",
                "coverage_report",
                "coverage report contract validation failed: {0}".format(exc),
            )
        )

    expected = release_validity.get("coverage_report_binding")
    expected = expected if isinstance(expected, Mapping) else None
    if expected is None:
        failures.append(
            _failure(
                "corpus_coverage",
                "MISSING_COVERAGE_BINDING",
                "release_validity.coverage_report_binding",
                "release evidence must bind the supplied coverage report identity",
            )
        )
    else:
        for field in COVERAGE_IDENTITY_FIELDS:
            if expected.get(field) != source.get(field):
                failures.append(
                    _failure(
                        "corpus_coverage",
                        "COVERAGE_IDENTITY_MISMATCH",
                        "coverage_report.{0}".format(field),
                        "coverage report identity does not match release evidence",
                    )
                )

    package_governance = release_validity.get("package_governance")
    package_governance = package_governance if isinstance(package_governance, Mapping) else None
    if package_governance is None:
        failures.append(
            _failure(
                "corpus_coverage",
                "MISSING_PACKAGE_GOVERNANCE",
                "release_validity.package_governance",
                "governed promotion requires the governance binding from the signed package manifest",
            )
        )
    else:
        expected_values = {
            "event_id": source.get("event_id"),
            "mission_profile.mission_profile_id": source.get("mission_profile_id"),
            "source_registry.registry_id": source.get("registry_id"),
            "coverage_report.report_id": source.get("report_id"),
        }
        observed_values = {
            "event_id": package_governance.get("event_id"),
            "mission_profile.mission_profile_id": (
                package_governance.get("mission_profile", {}).get("mission_profile_id")
                if isinstance(package_governance.get("mission_profile"), Mapping) else None
            ),
            "source_registry.registry_id": (
                package_governance.get("source_registry", {}).get("registry_id")
                if isinstance(package_governance.get("source_registry"), Mapping) else None
            ),
            "coverage_report.report_id": (
                package_governance.get("coverage_report", {}).get("report_id")
                if isinstance(package_governance.get("coverage_report"), Mapping) else None
            ),
        }
        if package_governance.get("contract_profile") != GOVERNED_PROFILE:
            failures.append(
                _failure(
                    "corpus_coverage",
                    "PROFILE_MISMATCH",
                    "release_validity.package_governance.contract_profile",
                    "signed package is not governed-v1",
                )
            )
        for path, expected_value in expected_values.items():
            if observed_values[path] != expected_value:
                failures.append(
                    _failure(
                        "corpus_coverage",
                        "COVERAGE_IDENTITY_MISMATCH",
                        "release_validity.package_governance." + path,
                        "signed package governance identity does not match coverage evidence",
                    )
                )
        for section in ("mission_profile", "source_registry", "coverage_report", "lineage_manifest"):
            value = package_governance.get(section)
            digest = value.get("digest") if isinstance(value, Mapping) else None
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                failures.append(
                    _failure(
                        "corpus_coverage",
                        "MISSING_OR_INVALID_BINDING",
                        f"release_validity.package_governance.{section}.digest",
                        "signed package governance digest is missing or invalid",
                    )
                )
        coverage_binding = package_governance.get("coverage_report")
        packaged_coverage_digest = (
            coverage_binding.get("digest")
            if isinstance(coverage_binding, Mapping)
            else None
        )
        if packaged_coverage_digest != object_digest(source):
            failures.append(
                _failure(
                    "corpus_coverage",
                    "COVERAGE_DIGEST_MISMATCH",
                    "release_validity.package_governance.coverage_report.digest",
                    "signed package coverage digest does not match supplied coverage evidence",
                )
            )
        lineage = package_governance.get("lineage_manifest")
        lineage_id = lineage.get("manifest_digest") if isinstance(lineage, Mapping) else None
        if not isinstance(lineage_id, str) or not _DIGEST_RE.fullmatch(lineage_id):
            failures.append(
                _failure(
                    "corpus_coverage",
                    "MISSING_OR_INVALID_BINDING",
                    "release_validity.package_governance.lineage_manifest.manifest_digest",
                    "lineage manifest identity is missing or invalid",
                )
            )

    suitability_event_id = suitability.get("event_id")
    if suitability_event_id is not None and suitability_event_id != source.get("event_id"):
        failures.append(
            _failure(
                "corpus_coverage",
                "COVERAGE_IDENTITY_MISMATCH",
                "coverage_report.event_id",
                "coverage report event does not match corpus suitability evidence",
            )
        )

    requirements = source.get("requirements")
    requirements = requirements if isinstance(requirements, list) else []
    blocked_requirements = [
        item.get("requirement_id") if isinstance(item, Mapping) else None
        for item in requirements
        if not isinstance(item, Mapping) or item.get("status") != "COVERED"
    ]
    if source.get("decision") != "COVERED":
        failures.append(
            _failure(
                "corpus_coverage",
                "COVERAGE_BLOCKED",
                "coverage_report.decision",
                "coverage report contains gaps or conflicts",
            )
        )
    if blocked_requirements:
        failures.append(
            _failure(
                "corpus_coverage",
                "REQUIREMENT_BLOCKED",
                "coverage_report.requirements",
                "every mission coverage requirement must be COVERED",
            )
        )

    return {
        field: source.get(field)
        for field in COVERAGE_IDENTITY_FIELDS
    } | {
        "decision": source.get("decision"),
        "requirement_count": len(requirements),
    }


def _safety_layer(
    layer: str,
    source: Mapping[str, Any],
    thresholds: Mapping[str, Mapping[str, Tuple[str, float]]],
    failures: List[Dict[str, str]],
) -> Dict[str, Any]:
    classes = source.get("safety_classes")
    classes = classes if isinstance(classes, Mapping) else {}
    class_results: Dict[str, Any] = {}
    policy_violations = source.get("policy_violations")
    if not _number(policy_violations):
        failures.append(
            _failure(
                layer,
                "MISSING_METRIC",
                f"{layer}.policy_violations",
                "sensitivity and channel policy violation count is required",
            )
        )
    elif policy_violations != 0:
        failures.append(
            _failure(
                layer,
                "HARD_GATE_FAILED",
                f"{layer}.policy_violations",
                "sensitivity and channel policy violations must be zero",
            )
        )
    for safety_class in SAFETY_CLASSES:
        values = classes.get(safety_class)
        values = values if isinstance(values, Mapping) else {}
        before = len(failures)
        case_count = values.get("case_count")
        if not isinstance(case_count, int) or isinstance(case_count, bool):
            failures.append(_failure(layer, "MISSING_CASE_COUNT", f"{layer}.safety_classes.{safety_class}.case_count", "sealed case count is required"))
        elif case_count < 50:
            failures.append(_failure(layer, "INSUFFICIENT_CASES", f"{layer}.safety_classes.{safety_class}.case_count", "at least 50 sealed cases are required"))
        checks = _check_thresholds(
            layer,
            f"{layer}.safety_classes.{safety_class}",
            values,
            thresholds[safety_class],
            failures,
        )
        failed_case_ids = values.get("failed_case_ids")
        if not isinstance(failed_case_ids, list) or not all(isinstance(item, str) for item in failed_case_ids):
            failures.append(_failure(layer, "MISSING_CASE_EVIDENCE", f"{layer}.safety_classes.{safety_class}.failed_case_ids", "failed-case evidence list is required"))
            failed_case_ids = None
        elif safety_class == "critical" and failed_case_ids:
            failures.append(_failure(layer, "CRITICAL_CASE_FAILED", f"{layer}.safety_classes.critical.failed_case_ids", "critical failures cannot be hidden by aggregate metrics"))
        class_results[safety_class] = {
            "case_count": case_count,
            "failed_case_ids": failed_case_ids,
            "checks": checks,
            "passed": len(failures) == before,
        }

    if source.get("deterministic_runs") != 3:
        failures.append(_failure(layer, "DETERMINISM_NOT_PROVEN", f"{layer}.deterministic_runs", "exactly three deterministic promotion runs are required"))
    proof_field = "rankings_identical" if layer == "retrieval" else "all_runs_passed"
    if source.get(proof_field) is not True:
        failures.append(_failure(layer, "DETERMINISM_NOT_PROVEN", f"{layer}.{proof_field}", "three-run determinism evidence did not pass"))
    return {
        "safety_classes": class_results,
        "policy_violations": policy_violations,
        "deterministic_runs": source.get("deterministic_runs"),
        proof_field: source.get(proof_field),
    }


def _answer_layer(source: Mapping[str, Any], failures: List[Dict[str, str]]) -> Dict[str, Any]:
    result = _safety_layer("grounded_answer", source, ANSWER_THRESHOLDS, failures)
    lineage = source.get("citation_lineage_resolution")
    if not _number(lineage):
        failures.append(_failure("grounded_answer", "MISSING_METRIC", "grounded_answer.citation_lineage_resolution", "universal citation-lineage metric is required"))
    elif lineage != 1.0:
        failures.append(_failure("grounded_answer", "HARD_GATE_FAILED", "grounded_answer.citation_lineage_resolution", "citation lineage must resolve for every factual answer"))
    result["citation_lineage_resolution"] = lineage
    return result


def _edge_layer(source: Mapping[str, Any], failures: List[Dict[str, str]]) -> Dict[str, Any]:
    profiles = source.get("profiles")
    profiles = profiles if isinstance(profiles, Mapping) else {}
    results: Dict[str, Any] = {}
    if not profiles:
        failures.append(_failure("edge_operation", "MISSING_PROFILES", "edge_profiles.profiles", "at least one declared edge profile is required"))
    for name in sorted(profiles):
        values = profiles[name]
        if not isinstance(values, Mapping):
            failures.append(_failure("edge_operation", "INVALID_PROFILE", f"edge_profiles.profiles.{name}", "profile evidence must be an object"))
            values = {}
        before = len(failures)
        checks = _check_thresholds("edge_operation", f"edge_profiles.profiles.{name}", values, EDGE_THRESHOLDS, failures)
        boolean_checks = {}
        for field in ("package_verification_passed", "index_build_passed", "restart_recovery_passed", "disconnected_smoke_passed"):
            passed = values.get(field) is True
            boolean_checks[field] = passed
            if not passed:
                failures.append(_failure("edge_operation", "PROFILE_CHECK_FAILED", f"edge_profiles.profiles.{name}.{field}", "required profile check did not pass"))
        llm_latency = values.get("llm_end_to_end_p95_ms")
        if not _number(llm_latency) or llm_latency < 0:
            failures.append(_failure("edge_operation", "MISSING_METRIC", f"edge_profiles.profiles.{name}.llm_end_to_end_p95_ms", "measured non-negative LLM p95 is required"))
        results[name] = {
            "checks": checks,
            "boolean_checks": boolean_checks,
            "llm_end_to_end_p95_ms": llm_latency,
            "passed": len(failures) == before,
        }
    return {"profiles": results}


def evaluate_promotion(
    release_validity: Mapping[str, Any],
    retrieval: Mapping[str, Any],
    grounded_answer: Mapping[str, Any],
    edge_profiles: Mapping[str, Any],
    suitability: Mapping[str, Any],
    coverage_report: Optional[Mapping[str, Any]] = None,
    *,
    contract_profile: str,
) -> Dict[str, Any]:
    """Evaluate release evidence and return a deterministic promotion report.

    The contract profile is always explicit. ``governed-v1`` requires complete
    coverage and signed-package governance bindings. ``legacy-v1`` preserves
    the historical five-layer decision for compatibility only.
    """

    evidence = {
        "release_validity": release_validity,
        "retrieval": retrieval,
        "grounded_answer": grounded_answer,
        "edge_profiles": edge_profiles,
    }
    failures: List[Dict[str, str]] = []
    for name, value in evidence.items():
        if not isinstance(value, Mapping):
            raise TypeError(f"{name} evidence must be a mapping")
    if not isinstance(suitability, Mapping):
        raise TypeError("suitability evidence must be a mapping")
    if coverage_report is not None and not isinstance(coverage_report, Mapping):
        raise TypeError("coverage_report evidence must be a mapping")
    if contract_profile not in CONTRACT_PROFILES:
        raise ValueError("contract_profile must explicitly select governed-v1 or legacy-v1")
    if contract_profile == LEGACY_PROFILE and coverage_report is not None:
        raise ValueError("legacy-v1 does not accept a coverage report")

    binding = _validate_bindings(evidence, failures)
    layer_failures_before = len(failures)
    suitability_result = _suitability_layer(suitability, binding, failures)
    suitability_result["passed"] = len(failures) == layer_failures_before

    coverage_result = None
    if contract_profile == GOVERNED_PROFILE and coverage_report is None:
        failures.append(
            _failure(
                "corpus_coverage",
                "MISSING_COVERAGE_REPORT",
                "coverage_report",
                "governed-v1 promotion requires a COVERED coverage report",
            )
        )
        coverage_result = {"passed": False}
    elif coverage_report is not None:
        layer_failures_before = len(failures)
        coverage_result = _coverage_layer(
            coverage_report, release_validity, suitability, failures
        )
        coverage_result["passed"] = len(failures) == layer_failures_before

    layer_failures_before = len(failures)
    release_result = _release_layer(release_validity, failures)
    release_result["passed"] = not any(item["layer"] == "release_validity" for item in failures[layer_failures_before:])

    layer_failures_before = len(failures)
    retrieval_result = _safety_layer("retrieval", retrieval, RETRIEVAL_THRESHOLDS, failures)
    retrieval_result["passed"] = len(failures) == layer_failures_before

    layer_failures_before = len(failures)
    answer_result = _answer_layer(grounded_answer, failures)
    answer_result["passed"] = len(failures) == layer_failures_before

    layer_failures_before = len(failures)
    edge_result = _edge_layer(edge_profiles, failures)
    edge_result["passed"] = len(failures) == layer_failures_before

    failures.sort(key=lambda item: (item["layer"], item["path"], item["code"], item["message"]))
    layers = {
        "corpus_suitability": suitability_result,
        "release_validity": release_result,
        "retrieval": retrieval_result,
        "grounded_answer": answer_result,
        "edge_operation": edge_result,
    }
    if coverage_result is not None:
        layers["corpus_coverage"] = coverage_result
    decision = "PASS" if not failures and all(layer["passed"] for layer in layers.values()) else "FAIL"
    report_body = {
        "schema_version": SCHEMA_VERSION,
        "spec_version": SPEC_VERSION,
        "contract_profile": contract_profile,
        "binding": binding,
        "layers": layers,
        "failures": failures,
        "decision": decision,
    }
    report_id = "sha256:" + hashlib.sha256(canonical_json(report_body).encode("utf-8")).hexdigest()
    return {"report_id": report_id, **report_body}


def load_json_document(path: str) -> Dict[str, Any]:
    """Load a JSON object for the command-line evaluator."""

    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value
