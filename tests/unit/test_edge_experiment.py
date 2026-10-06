"""Tests for controlled edge experiment comparisons."""

import pytest

from tests.benchmarks.edge_experiment import (
    comparison_rows,
    latency_summary,
    validate_comparable,
)


def _report(scenario, model, corpus="sha256:corpus", profile="lab-small"):
    return {
        "identity": {
            "scenario": scenario,
            "comparison_group": "small-edge",
            "provider": "test",
            "model": model,
            "runtime": "runtime",
            "profile": profile,
            "corpus_digest": corpus,
            "embedding_model": "minilm",
            "eval_set_digest": "sha256:eval",
            "top_k": 3,
        },
        "measurements": {
            "pipeline_quality": {"pass_rate": 0.8},
            "pipeline_latency": {"p50_ms": 100, "p95_ms": 200},
            "retrieval": {"mean_evidence_recall_at_k": 0.9},
            "request_failures": [],
        },
    }


def test_latency_summary_uses_nearest_rank_percentiles():
    summary = latency_summary([40, 10, 30, 20])
    assert summary["p50_ms"] == 20
    assert summary["p95_ms"] == 40


def test_comparison_accepts_model_as_the_experimental_variable():
    reports = [_report("control", "bitnet"), _report("candidate", "candidate-2b")]

    rows = comparison_rows(reports)

    assert [row["model"] for row in rows] == ["bitnet", "candidate-2b"]


def test_comparison_rejects_different_corpus_or_resource_profile():
    baseline = _report("control", "bitnet")

    with pytest.raises(ValueError, match="corpus_digest"):
        validate_comparable([baseline, _report("candidate", "model", corpus="other")])
    with pytest.raises(ValueError, match="profile"):
        validate_comparable(
            [baseline, _report("candidate", "model", profile="lab-balanced")]
        )
