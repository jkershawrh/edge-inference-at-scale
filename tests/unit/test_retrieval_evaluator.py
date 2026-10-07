"""Tests for the LLM-independent retrieval evaluation metrics."""

import yaml

from tests.evaluation.run_retrieval_eval import (
    _percentile,
    load_retrieval_specs,
    score_query,
)


def test_score_query_measures_evidence_recall_and_rank():
    spec = {
        "expected_keywords": ["first aid", "North Lobby"],
        "expected_entities": ["Level 1"],
    }
    documents = [
        "Registration details are available here.",
        "First aid is in the North Lobby on Level 1.",
    ]

    result = score_query(spec, documents)

    assert result["evidence_recall"] == 1.0
    assert result["first_relevant_rank"] == 2
    assert result["reciprocal_rank"] == 0.5


def test_score_query_reports_missing_evidence():
    spec = {"expected_keywords": ["BitNet", "A2"], "expected_entities": []}

    result = score_query(spec, ["An unrelated session is in room B1."])

    assert result["evidence_recall"] == 0.0
    assert result["reciprocal_rank"] == 0.0
    assert result["misses"] == ["BitNet", "A2"]


def test_percentile_uses_nearest_rank():
    assert _percentile([40.0, 10.0, 30.0, 20.0], 0.50) == 20.0
    assert _percentile([40.0, 10.0, 30.0, 20.0], 0.95) == 40.0


def test_retrieval_scope_excludes_stateful_application_commands(tmp_path):
    path = tmp_path / "queries.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "retrieval_categories": ["venue"],
                "eval_queries": [
                    {"id": "venue", "category": "venue"},
                    {"id": "command", "category": "treasure_hunt"},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert [item["id"] for item in load_retrieval_specs(path)] == ["venue"]
