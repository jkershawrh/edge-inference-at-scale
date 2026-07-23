"""Unit tests for the EDD ResponseEvaluator.

Tests cover scoring logic for keywords, entities, forbidden words,
SMS length, latency, and repetition detection.

All tests use inline query specs — no live services needed.
"""

import os
import sys
import tempfile
import textwrap

import pytest
import yaml

# Allow importing evaluator from the evaluation package
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "evaluation")
)

from evaluator import ResponseEvaluator  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_queries(queries: list[dict], tmp_path: str) -> str:
    """Write a minimal eval_queries.yaml and return its path."""
    path = os.path.join(tmp_path, "eval_queries.yaml")
    with open(path, "w") as fh:
        yaml.dump({"eval_queries": queries}, fh)
    return path


def _make_evaluator(queries: list[dict], tmp_path: str) -> ResponseEvaluator:
    path = _write_queries(queries, tmp_path)
    return ResponseEvaluator(queries_file=path)


BASIC_QUERY = {
    "id": "test_01",
    "query": "What time is the keynote?",
    "category": "schedule",
    "expected_keywords": ["9:00", "keynote", "Main Hall"],
    "forbidden_keywords": ["I don't know", "unfortunately"],
    "expected_entities": ["9:00", "Main Hall", "Day 1"],
    "max_acceptable_latency_ms": 15000,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestScoreResponse:
    """Tests for ResponseEvaluator.score_response."""

    def test_perfect_response_scores_high(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))
        response = "The keynote is at 9:00 in Main Hall on Day 1."
        result = evaluator.score_response("test_01", response, latency_ms=500.0)

        assert result["keyword_score"] == 1.0
        assert result["entity_score"] == 1.0
        assert result["forbidden_score"] == 1.0
        assert result["length_score"] == 1.0
        assert result["latency_score"] == 1.0
        assert result["repetition_score"] == 1.0
        assert result["composite_score"] == 1.0
        assert result["pass"] is True

    def test_empty_response_scores_low(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))
        result = evaluator.score_response("test_01", "", latency_ms=500.0)

        assert result["keyword_score"] == 0.0
        assert result["entity_score"] == 0.0
        # forbidden_score is 1.0 because no forbidden words are found in empty
        assert result["forbidden_score"] == 1.0
        assert result["length_score"] == 1.0  # 0 chars < 160
        # composite = 0.15 + 0.10 + 0.10 + 0.15 = 0.50 (borderline pass)
        assert result["composite_score"] <= 0.5

    def test_partial_keyword_match(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))
        # Only "keynote" matches, "9:00" and "Main Hall" do not
        response = "The keynote starts in the morning."
        result = evaluator.score_response("test_01", response, latency_ms=500.0)

        assert result["keyword_score"] == pytest.approx(1 / 3, abs=0.01)
        assert "keynote" in result["details"]["keyword_hits"]
        assert "9:00" in result["details"]["keyword_misses"]

    def test_over_160_chars_penalized(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))
        long_response = "The keynote is at 9:00 in Main Hall on Day 1. " + "x" * 160
        assert len(long_response) > 160

        result = evaluator.score_response("test_01", long_response, latency_ms=500.0)
        assert result["length_score"] == 0.0
        assert result["composite_score"] < 1.0

    def test_forbidden_keyword_penalized(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))
        response = "I don't know about the keynote at 9:00 in Main Hall on Day 1."
        result = evaluator.score_response("test_01", response, latency_ms=500.0)

        assert result["forbidden_score"] == 0.0
        assert "I don't know" in result["details"]["forbidden_found"]

    def test_latency_over_threshold_penalized(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))
        response = "The keynote is at 9:00 in Main Hall on Day 1."

        # Exactly at threshold: full score
        result_at = evaluator.score_response("test_01", response, latency_ms=15000.0)
        assert result_at["latency_score"] == 1.0

        # At 1.5x threshold: half score
        result_mid = evaluator.score_response("test_01", response, latency_ms=22500.0)
        assert result_mid["latency_score"] == pytest.approx(0.5, abs=0.01)

        # At 2x threshold: zero
        result_over = evaluator.score_response("test_01", response, latency_ms=30000.0)
        assert result_over["latency_score"] == 0.0

    def test_case_insensitive_matching(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))
        response = "The KEYNOTE is at 9:00 in MAIN HALL on DAY 1."
        result = evaluator.score_response("test_01", response, latency_ms=500.0)

        assert result["keyword_score"] == 1.0
        assert result["entity_score"] == 1.0

    def test_unknown_query_id_raises(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))
        with pytest.raises(KeyError, match="nonexistent"):
            evaluator.score_response("nonexistent", "anything", 100.0)


class TestScoreRepetition:
    """Tests for the repetition detection scorer."""

    def test_no_repetition_scores_one(self):
        text = "The keynote is at 9:00 in the Main Hall on Day 1."
        assert ResponseEvaluator.score_repetition(text) == 1.0

    def test_repetitive_response_penalized(self):
        # Heavily repetitive text — same bigrams and trigrams repeated many times
        text = (
            "the keynote the keynote the keynote the keynote "
            "the keynote the keynote the keynote the keynote"
        )
        score = ResponseEvaluator.score_repetition(text)
        assert score < 0.5, f"Expected score < 0.5 for repetitive text, got {score}"

    def test_empty_string_scores_one(self):
        assert ResponseEvaluator.score_repetition("") == 1.0

    def test_short_string_scores_one(self):
        assert ResponseEvaluator.score_repetition("Hi") == 1.0


class TestCompositeWeights:
    """Verify the composite score uses configurable weights."""

    def test_composite_score_weights(self, tmp_path):
        # All weight on keyword — entity, length, etc. should not matter
        custom_weights = {
            "keyword": 1.0,
            "entity": 0.0,
            "forbidden": 0.0,
            "length": 0.0,
            "latency": 0.0,
            "repetition": 0.0,
        }
        path = _write_queries([BASIC_QUERY], str(tmp_path))
        evaluator = ResponseEvaluator(queries_file=path, weights=custom_weights)

        # Response with all keywords but missing entities
        response = "keynote at 9:00 in Main Hall."
        result = evaluator.score_response("test_01", response, latency_ms=500.0)

        # keyword_score should be 1.0 (all keywords match)
        assert result["keyword_score"] == 1.0
        # composite should equal keyword_score since all other weights are 0
        assert result["composite_score"] == pytest.approx(1.0, abs=0.01)

    def test_default_weights_sum_to_one(self):
        from evaluator import DEFAULT_WEIGHTS

        assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0, abs=0.001)


class TestRunEvaluation:
    """Tests for the batch run_evaluation method."""

    def test_batch_evaluation(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))

        responses = [
            {
                "query_id": "test_01",
                "response": "The keynote is at 9:00 in Main Hall on Day 1.",
                "latency_ms": 500.0,
            },
        ]

        report = evaluator.run_evaluation(responses)
        assert report["total_queries"] == 1
        assert report["passed"] == 1
        assert report["failed"] == 0
        assert report["pass_rate"] == 1.0
        assert "schedule" in report["category_scores"]

    def test_empty_batch(self, tmp_path):
        evaluator = _make_evaluator([BASIC_QUERY], str(tmp_path))

        report = evaluator.run_evaluation([])
        assert report["total_queries"] == 0
        assert report["pass_rate"] == 0.0
        assert report["results"] == []

    def test_mixed_pass_fail(self, tmp_path):
        queries = [
            BASIC_QUERY,
            {
                "id": "test_02",
                "query": "Where is the venue?",
                "category": "venue",
                "expected_keywords": ["Convention Center"],
                "forbidden_keywords": ["I don't know"],
                "expected_entities": ["100 Main Street"],
                "max_acceptable_latency_ms": 15000,
            },
        ]
        evaluator = _make_evaluator(queries, str(tmp_path))

        responses = [
            {
                "query_id": "test_01",
                "response": "The keynote is at 9:00 in Main Hall on Day 1.",
                "latency_ms": 500.0,
            },
            {
                "query_id": "test_02",
                # Wrong answer with forbidden content AND excessive latency
                # to push composite below 0.5
                "response": "I don't know unfortunately",
                "latency_ms": 45000.0,  # 3x the threshold -> latency_score = 0.0
            },
        ]

        report = evaluator.run_evaluation(responses)
        assert report["total_queries"] == 2
        assert report["passed"] == 1
        assert report["failed"] == 1
        assert report["pass_rate"] == 0.5
