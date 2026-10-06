#!/usr/bin/env python3
"""Evaluate retrieval quality without involving the LLM or SMS transport.

This isolates whether the edge knowledge base found the supporting evidence.
It reports evidence recall, reciprocal rank, and retrieval latency against the
same ground-truth facts used by the end-to-end response evaluation.
"""

import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


RAG_URL = os.environ.get("RAG_EVAL_URL", "http://localhost:8004")
TOP_K = int(os.environ.get("RAG_EVAL_TOP_K", "3"))
RECALL_GATE = float(os.environ.get("RAG_EVAL_RECALL_GATE", "0.70"))
QUERY_FILE = Path(__file__).parent / "eval_queries.yaml"
RESULTS_FILE = Path(__file__).parent / "retrieval_results.json"


def _percentile(values: List[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def search(query: str) -> Tuple[List[str], List[float], float]:
    payload = json.dumps({"query": query, "top_k": TOP_K}).encode("utf-8")
    request = urllib.request.Request(
        "{0}/search".format(RAG_URL.rstrip("/")),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started_at = time.monotonic()
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    latency_ms = (time.monotonic() - started_at) * 1000
    return body.get("documents", []), body.get("scores", []), latency_ms


def score_query(spec: Dict[str, Any], documents: List[str]) -> Dict[str, Any]:
    evidence = list(
        dict.fromkeys(
            spec.get("expected_keywords", []) + spec.get("expected_entities", [])
        )
    )
    documents_lower = [document.lower() for document in documents]
    hits = [
        item
        for item in evidence
        if any(item.lower() in document for document in documents_lower)
    ]
    misses = [item for item in evidence if item not in hits]
    recall = len(hits) / len(evidence) if evidence else 1.0

    first_relevant_rank = None
    for index, document in enumerate(documents_lower, start=1):
        if any(item.lower() in document for item in evidence):
            first_relevant_rank = index
            break
    reciprocal_rank = 1.0 / first_relevant_rank if first_relevant_rank else 0.0
    return {
        "evidence_recall": recall,
        "reciprocal_rank": reciprocal_rank,
        "first_relevant_rank": first_relevant_rank,
        "hits": hits,
        "misses": misses,
    }


def main() -> int:
    with open(QUERY_FILE, "r", encoding="utf-8") as handle:
        specs = yaml.safe_load(handle).get("eval_queries", [])

    results = []
    try:
        for spec in specs:
            documents, scores, latency_ms = search(spec["query"])
            scored = score_query(spec, documents)
            result = {
                "query_id": spec["id"],
                "query": spec["query"],
                "category": spec.get("category", "unknown"),
                "latency_ms": latency_ms,
                "scores": scores,
                "documents": documents,
                **scored,
            }
            results.append(result)
            print(
                "[{0}] recall={1:.0%} rr={2:.2f} latency={3:.0f}ms".format(
                    spec["id"],
                    scored["evidence_recall"],
                    scored["reciprocal_rank"],
                    latency_ms,
                )
            )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print("RAG evaluation could not reach {0}: {1}".format(RAG_URL, exc))
        return 2

    recalls = [item["evidence_recall"] for item in results]
    reciprocal_ranks = [item["reciprocal_rank"] for item in results]
    latencies = [item["latency_ms"] for item in results]
    mean_recall = statistics.mean(recalls) if recalls else 0.0
    report = {
        "rag_url": RAG_URL,
        "top_k": TOP_K,
        "query_count": len(results),
        "mean_evidence_recall_at_k": mean_recall,
        "mean_reciprocal_rank": statistics.mean(reciprocal_ranks)
        if reciprocal_ranks
        else 0.0,
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies) if latencies else 0.0,
        },
        "gate": RECALL_GATE,
        "passed": mean_recall >= RECALL_GATE,
        "results": results,
    }
    with open(RESULTS_FILE, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    print("\nRetrieval evaluation")
    print("  Recall@{0}: {1:.1%}".format(TOP_K, report["mean_evidence_recall_at_k"]))
    print("  MRR:      {0:.3f}".format(report["mean_reciprocal_rank"]))
    print(
        "  p50/p95:  {0:.0f}/{1:.0f} ms".format(
            report["latency_ms"]["p50"], report["latency_ms"]["p95"]
        )
    )
    print("  Gate:     {0}".format("PASS" if report["passed"] else "FAIL"))
    print("  Results:  {0}".format(RESULTS_FILE))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
