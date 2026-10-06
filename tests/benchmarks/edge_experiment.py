"""Pure helpers for comparable OpenShift edge experiments."""

import hashlib
import json
import math
import statistics
from typing import Any, Dict, Iterable, List


COMPARABILITY_FIELDS = (
    "comparison_group",
    "profile",
    "corpus_digest",
    "embedding_model",
    "eval_set_digest",
    "top_k",
)


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def percentile(values: Iterable[float], percentile_value: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil((percentile_value / 100.0) * len(ordered)) - 1)
    return ordered[index]


def latency_summary(values: List[float]) -> Dict[str, float]:
    return {
        "samples": len(values),
        "mean_ms": statistics.mean(values) if values else 0.0,
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "max_ms": max(values) if values else 0.0,
    }


def validate_comparable(reports: List[Dict[str, Any]]) -> None:
    """Reject model comparisons where non-model experiment controls differ."""
    if len(reports) < 2:
        raise ValueError("at least two experiment reports are required")
    baseline = reports[0].get("identity") or {}
    for report in reports[1:]:
        identity = report.get("identity") or {}
        mismatches = [
            field
            for field in COMPARABILITY_FIELDS
            if identity.get(field) != baseline.get(field)
        ]
        if mismatches:
            raise ValueError(
                "experiment controls differ: {0}".format(", ".join(mismatches))
            )


def comparison_rows(reports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    validate_comparable(reports)
    rows = []
    for report in reports:
        identity = report["identity"]
        measurements = report["measurements"]
        rows.append(
            {
                "scenario": identity["scenario"],
                "model": identity["model"],
                "provider": identity["provider"],
                "runtime": identity["runtime"],
                "quality_pass_rate": measurements["pipeline_quality"]["pass_rate"],
                "retrieval_recall_at_k": measurements["retrieval"][
                    "mean_evidence_recall_at_k"
                ],
                "pipeline_p50_ms": measurements["pipeline_latency"]["p50_ms"],
                "pipeline_p95_ms": measurements["pipeline_latency"]["p95_ms"],
                "failures": measurements["request_failures"],
            }
        )
    return rows


def load_report(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
