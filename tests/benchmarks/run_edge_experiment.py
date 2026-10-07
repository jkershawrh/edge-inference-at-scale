#!/usr/bin/env python3
"""Run one controlled RAG + LLM experiment against an OpenShift deployment."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.benchmarks.edge_experiment import file_sha256, latency_summary
from tests.evaluation.evaluator import ResponseEvaluator
from tests.evaluation.run_retrieval_eval import score_query


HUNT_SENDER = "+15550010000"
HUNT_HINT_SENDER = "+15550010001"
HUNT_LOCKED_SENDER = "+15550010002"


def experiment_sender(query_id: str, index: int) -> str:
    """Keep stateful journeys isolated while retaining deterministic senders."""
    if query_id == "hunt_hint":
        return HUNT_HINT_SENDER
    if query_id == "hunt_clue_locked":
        return HUNT_LOCKED_SENDER
    if query_id.startswith("hunt_"):
        return HUNT_SENDER
    return "+1556{0:07d}".format(index)


def counter_delta(after: Dict[str, Any], before: Dict[str, Any], key: str) -> int:
    return max(0, int(after.get(key, 0)) - int(before.get(key, 0)))


def request_json(
    base_url: str,
    path: str,
    timeout: float,
    payload: Dict[str, Any] = None,
) -> Tuple[Dict[str, Any], float]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        "{0}{1}".format(base_url.rstrip("/"), path),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    started_at = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body, (time.monotonic() - started_at) * 1000


def resolve_scenario(matrix_path: str, scenario_name: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    with open(matrix_path, "r", encoding="utf-8") as handle:
        matrix = yaml.safe_load(handle)
    scenarios = matrix.get("scenarios") or {}
    if scenario_name not in scenarios:
        raise ValueError("unknown scenario: {0}".format(scenario_name))
    scenario = dict(matrix.get("defaults") or {})
    scenario.update(scenarios[scenario_name])
    scenario["scenario"] = scenario_name
    return matrix, scenario


def run(matrix_path: str, scenario_name: str, output_path: str) -> Dict[str, Any]:
    _, scenario = resolve_scenario(matrix_path, scenario_name)
    api_url = os.environ.get(scenario["api_url_env"])
    corpus_digest = os.environ.get(scenario["corpus_digest_env"])
    if not api_url or not corpus_digest:
        raise ValueError(
            "set {0} and {1} before running".format(
                scenario["api_url_env"], scenario["corpus_digest_env"]
            )
        )
    if scenario["model"].startswith("SET_") or scenario["runtime"].startswith("SET_"):
        raise ValueError("replace candidate model and runtime placeholders first")

    queries_file = scenario["eval_queries"]
    evaluator = ResponseEvaluator(queries_file=queries_file)
    timeout = float(scenario["request_timeout_seconds"])
    top_k = int(scenario["top_k"])
    pipeline_responses = []
    pipeline_latencies = []
    retrieval_scores = []
    retrieval_latencies = []
    failures = []

    health, _ = request_json(api_url, "/services/health", timeout)
    llm_health, _ = request_json(api_url, "/llm/health", timeout)
    rag_stats, _ = request_json(api_url, "/rag/stats", timeout)
    llm_stats_before, _ = request_json(api_url, "/llm/stats", timeout)
    response_modes: Dict[str, int] = {}

    for index, (query_id, spec) in enumerate(evaluator.queries.items(), start=1):
        try:
            rag_body, rag_latency = request_json(
                api_url,
                "/rag/search",
                timeout,
                {"query": spec["query"], "top_k": top_k},
            )
            retrieval_latencies.append(rag_latency)
            retrieval_scores.append(score_query(spec, rag_body.get("documents", [])))

            sender = experiment_sender(query_id, index)
            if query_id in {"hunt_hint", "hunt_clue_locked"}:
                request_json(
                    api_url,
                    "/router/route",
                    timeout,
                    {
                        "sender": sender,
                        "receiver": "+15559876543",
                        "content": "HUNT",
                        "channel": "simulator",
                    },
                )
            pipeline_body, pipeline_latency = request_json(
                api_url,
                "/router/route",
                timeout,
                {
                    "sender": sender,
                    "receiver": "+15559876543",
                    "content": spec["query"],
                    "channel": "simulator",
                },
            )
            pipeline_latencies.append(pipeline_latency)
            response_mode = (pipeline_body.get("attribution") or {}).get(
                "response_mode", "unattributed"
            )
            response_modes[response_mode] = response_modes.get(response_mode, 0) + 1
            pipeline_responses.append(
                {
                    "query_id": query_id,
                    "query": spec["query"],
                    "response": pipeline_body.get("response", pipeline_body.get("message", "")),
                    "latency_ms": pipeline_latency,
                }
            )
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            failures.append({"query_id": query_id, "error": str(exc)})

    quality = evaluator.run_evaluation(pipeline_responses)
    expected_queries = len(evaluator.queries)
    quality["request_completion_rate"] = (
        len(pipeline_responses) / expected_queries if expected_queries else 0.0
    )
    quality["total_queries"] = expected_queries
    quality["failed"] = expected_queries - quality["passed"]
    quality["pass_rate"] = (
        round(quality["passed"] / expected_queries, 4) if expected_queries else 0.0
    )
    mean_recall = (
        sum(item["evidence_recall"] for item in retrieval_scores) / expected_queries
        if expected_queries
        else 0.0
    )
    mean_rr = (
        sum(item["reciprocal_rank"] for item in retrieval_scores) / expected_queries
        if expected_queries
        else 0.0
    )
    observed_llm = (llm_health.get("details") or {}).get("model_name")
    llm_stats_after, _ = request_json(api_url, "/llm/stats", timeout)
    llm_requests_delta = counter_delta(
        llm_stats_after, llm_stats_before, "requests_total"
    )
    llm_successes_delta = counter_delta(
        llm_stats_after, llm_stats_before, "requests_successful"
    )
    report = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "identity": {
            "scenario": scenario_name,
            "comparison_group": scenario["comparison_group"],
            "provider": scenario["provider"],
            "model": scenario["model"],
            "runtime": scenario["runtime"],
            "profile": scenario["profile"],
            "corpus_digest": corpus_digest,
            "embedding_model": scenario["embedding_model"],
            "eval_set_digest": file_sha256(queries_file),
            "top_k": top_k,
        },
        "measurements": {
            "pipeline_quality": quality,
            "pipeline_latency": latency_summary(pipeline_latencies),
            "retrieval": {
                "mean_evidence_recall_at_k": mean_recall,
                "mean_reciprocal_rank": mean_rr,
                "latency": latency_summary(retrieval_latencies),
            },
            "request_failures": failures,
            "health_snapshot": health,
            "llm_health": llm_health,
            "rag_stats": rag_stats,
            "llm_stats_before": llm_stats_before,
            "llm_stats_after": llm_stats_after,
            "model_contribution": {
                "response_modes": response_modes,
                "llm_requests_delta": llm_requests_delta,
                "llm_successes_delta": llm_successes_delta,
            },
        },
        "gates": {
            "quality_passed": quality["pass_rate"] >= float(scenario["quality_gate"]),
            "retrieval_passed": mean_recall >= float(scenario["retrieval_recall_gate"]),
            "requests_passed": not failures,
            "model_identity_passed": observed_llm == scenario["model"],
            "model_exercised": llm_requests_delta > 0,
        },
    }
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", default="tests/benchmarks/experiment_matrix.yaml")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        report = run(args.matrix, args.scenario, args.output)
    except (ValueError, urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print("Experiment failed: {0}".format(exc))
        return 2
    print(json.dumps({"identity": report["identity"], "gates": report["gates"]}, indent=2))
    return 0 if all(report["gates"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
