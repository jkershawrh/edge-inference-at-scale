"""Response Evaluator — EDD framework for Edge Inference at Scale.

Scores LLM responses from the SMS pipeline against ground-truth
expected keywords, entities, and quality constraints defined in
eval_queries.yaml.

Uses only string matching — no ML-based evaluation.
"""

import logging
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Default weights for the composite score
DEFAULT_WEIGHTS = {
    "keyword": 0.25,
    "entity": 0.25,
    "forbidden": 0.15,
    "length": 0.10,
    "latency": 0.10,
    "repetition": 0.15,
}

PASS_THRESHOLD = 0.5
SMS_MAX_CHARS = 160


class ResponseEvaluator:
    """Score SMS pipeline responses against ground-truth eval queries."""

    def __init__(
        self,
        queries_file: Optional[str] = None,
        weights: Optional[Dict[str, float]] = None,
    ):
        if queries_file is None:
            queries_file = str(
                Path(__file__).parent / "eval_queries.yaml"
            )
        with open(queries_file, "r") as fh:
            data = yaml.safe_load(fh)

        self.queries: Dict[str, Dict[str, Any]] = {}
        for q in data.get("eval_queries", []):
            self.queries[q["id"]] = q

        self.weights = weights or dict(DEFAULT_WEIGHTS)

        # Normalise weights so they sum to 1.0
        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: v / total for k, v in self.weights.items()}

        logger.info("Loaded %d eval queries from %s", len(self.queries), queries_file)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_response(
        self, query_id: str, response: str, latency_ms: float
    ) -> Dict[str, Any]:
        """Score a single response against its ground truth.

        Returns a dict with per-dimension scores, a weighted composite,
        a pass/fail flag, and detail about what matched and what missed.
        """
        if query_id not in self.queries:
            raise KeyError(f"Unknown query_id: {query_id}")

        spec = self.queries[query_id]
        response_lower = response.lower()

        # --- keyword score ---
        expected_kw = spec.get("expected_keywords", [])
        kw_hits = [kw for kw in expected_kw if kw.lower() in response_lower]
        kw_misses = [kw for kw in expected_kw if kw.lower() not in response_lower]
        keyword_score = len(kw_hits) / len(expected_kw) if expected_kw else 1.0

        # --- entity score ---
        expected_ent = spec.get("expected_entities", [])
        ent_hits = [e for e in expected_ent if e.lower() in response_lower]
        ent_misses = [e for e in expected_ent if e.lower() not in response_lower]
        entity_score = len(ent_hits) / len(expected_ent) if expected_ent else 1.0

        # --- forbidden score ---
        forbidden = spec.get("forbidden_keywords", [])
        forbidden_found = [
            f for f in forbidden if f.lower() in response_lower
        ]
        forbidden_score = (
            1.0 if not forbidden_found else 0.0
        )

        # --- length score (SMS compliance) ---
        length_score = 1.0 if len(response) <= SMS_MAX_CHARS else 0.0

        # --- latency score ---
        max_latency = spec.get("max_acceptable_latency_ms", 15000)
        if latency_ms <= max_latency:
            latency_score = 1.0
        elif latency_ms <= max_latency * 2:
            # Linear decay from 1.0 to 0.0 between threshold and 2x threshold
            latency_score = 1.0 - (latency_ms - max_latency) / max_latency
        else:
            latency_score = 0.0

        # --- repetition score ---
        repetition_score = self.score_repetition(response)

        # --- composite ---
        composite = (
            self.weights["keyword"] * keyword_score
            + self.weights["entity"] * entity_score
            + self.weights["forbidden"] * forbidden_score
            + self.weights["length"] * length_score
            + self.weights["latency"] * latency_score
            + self.weights["repetition"] * repetition_score
        )

        return {
            "query_id": query_id,
            "keyword_score": round(keyword_score, 4),
            "entity_score": round(entity_score, 4),
            "forbidden_score": round(forbidden_score, 4),
            "length_score": round(length_score, 4),
            "latency_score": round(latency_score, 4),
            "repetition_score": round(repetition_score, 4),
            "composite_score": round(composite, 4),
            "pass": composite >= PASS_THRESHOLD,
            "details": {
                "keyword_hits": kw_hits,
                "keyword_misses": kw_misses,
                "entity_hits": ent_hits,
                "entity_misses": ent_misses,
                "forbidden_found": forbidden_found,
                "response_length": len(response),
                "latency_ms": latency_ms,
            },
        }

    @staticmethod
    def score_repetition(text: str) -> float:
        """Detect repeated phrases in *text*.

        Counts bigram and trigram repetitions.  A response with no
        repetition scores 1.0; heavily repetitive text scores toward 0.0.
        """
        if not text:
            return 1.0

        words = re.findall(r"\w+", text.lower())
        if len(words) < 4:
            return 1.0

        def _ngram_penalty(tokens: list[str], n: int) -> float:
            if len(tokens) < n:
                return 0.0
            ngrams = [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]
            counts = Counter(ngrams)
            repeated = sum(c - 1 for c in counts.values() if c > 2)
            total = len(ngrams)
            if total == 0:
                return 0.0
            return repeated / total

        bigram_pen = _ngram_penalty(words, 2)
        trigram_pen = _ngram_penalty(words, 3)

        # Average the two penalties, clamp to [0, 1]
        penalty = min(1.0, (bigram_pen + trigram_pen) / 2)
        return round(1.0 - penalty, 4)

    def run_evaluation(
        self, responses: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Score a batch of responses and compute aggregate metrics.

        *responses* is a list of dicts, each containing:
          - ``query_id``  (str)
          - ``response``  (str)
          - ``latency_ms`` (float)

        Returns aggregate metrics plus per-query results.
        """
        results: List[Dict[str, Any]] = []
        for item in responses:
            result = self.score_response(
                query_id=item["query_id"],
                response=item["response"],
                latency_ms=item.get("latency_ms", 0.0),
            )
            results.append(result)

        if not results:
            return {
                "total_queries": 0,
                "passed": 0,
                "failed": 0,
                "pass_rate": 0.0,
                "avg_composite_score": 0.0,
                "avg_keyword_score": 0.0,
                "avg_latency_ms": 0.0,
                "category_scores": {},
                "results": [],
            }

        passed = sum(1 for r in results if r["pass"])
        failed = len(results) - passed

        avg_composite = sum(r["composite_score"] for r in results) / len(results)
        avg_keyword = sum(r["keyword_score"] for r in results) / len(results)
        avg_latency = sum(
            item.get("latency_ms", 0.0) for item in responses
        ) / len(responses)

        # Per-category scores
        category_scores: Dict[str, List[float]] = {}
        for item, result in zip(responses, results):
            qid = item["query_id"]
            cat = self.queries[qid].get("category", "unknown")
            category_scores.setdefault(cat, []).append(result["composite_score"])

        cat_avg = {
            cat: round(sum(scores) / len(scores), 4)
            for cat, scores in category_scores.items()
        }

        return {
            "total_queries": len(results),
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / len(results), 4),
            "avg_composite_score": round(avg_composite, 4),
            "avg_keyword_score": round(avg_keyword, 4),
            "avg_latency_ms": round(avg_latency, 2),
            "category_scores": cat_avg,
            "results": results,
        }
