#!/usr/bin/env python3
"""Run the EDD evaluation suite against the live SMS pipeline.

Sends each eval query through the API, collects responses and latency,
scores them with ResponseEvaluator, prints a report, and writes
results to tests/evaluation/results.json.

Exit code:
    0  if pass_rate >= 0.6
    1  otherwise

Environment variables:
    EVAL_API_URL       Base URL for the SMS pipeline API.
                       Default: http://localhost:8000
    EVAL_RESULTS_FILE  Output path for retained evidence.
                       Default: tests/evaluation/results.json
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from evaluator import ResponseEvaluator

logger = logging.getLogger(__name__)

API_URL = os.environ.get("EVAL_API_URL", "http://localhost:8000")
PASS_RATE_GATE = 0.8
RESULTS_FILE = Path(
    os.environ.get(
        "EVAL_RESULTS_FILE", str(Path(__file__).parent / "results.json")
    )
)


_eval_sender_counter = 0
HUNT_SENDER = "+15550000000"
HUNT_HINT_SENDER = "+15550000001"
HUNT_LOCKED_SENDER = "+15550000002"


def send_query(query_text: str, sender: str = None) -> tuple[str, float]:
    """Send *query_text* to the SMS pipeline and return (response, latency_ms)."""
    if sender is None:
        global _eval_sender_counter
        _eval_sender_counter += 1
        sender = f"+1555{_eval_sender_counter:07d}"
    payload = json.dumps({
        "sender": sender,
        "receiver": "+15559876543",
        "content": query_text,
    }).encode("utf-8")
    url = f"{API_URL}/router/route"

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.error("Request failed for %r: %s", query_text, exc)
        return "", 0.0

    latency_ms = (time.monotonic() - start) * 1000
    response_text = body.get("response", body.get("message", ""))
    return response_text, latency_ms


def print_report(report: dict) -> None:
    """Print a human-readable evaluation report."""
    print("\n" + "=" * 72)
    print("  EDD Evaluation Report — Edge Inference at Scale")
    print("=" * 72)

    print(f"\n  Total queries : {report['total_queries']}")
    print(f"  Passed        : {report['passed']}")
    print(f"  Failed        : {report['failed']}")
    print(f"  Pass rate     : {report['pass_rate']:.1%}")
    print(f"  Avg composite : {report['avg_composite_score']:.3f}")
    print(f"  Avg keyword   : {report['avg_keyword_score']:.3f}")
    print(f"  Avg latency   : {report['avg_latency_ms']:.0f} ms")

    print("\n  Category scores:")
    for cat, score in sorted(report.get("category_scores", {}).items()):
        pass_rate = report.get("category_pass_rates", {}).get(cat, 0.0)
        print(f"    {cat:<12s}  score={score:.3f} pass={pass_rate:.1%}")

    print("\n  Per-query results:")
    print(f"  {'ID':<12s} {'Pass':>5s} {'Comp':>6s} {'KW':>6s} "
          f"{'Ent':>6s} {'Forb':>6s} {'Len':>5s} {'Lat':>6s} {'Rep':>5s}")
    print("  " + "-" * 68)

    for r in report["results"]:
        status = "PASS" if r["pass"] else "FAIL"
        print(
            f"  {r['query_id']:<12s} {status:>5s} "
            f"{r['composite_score']:>6.3f} "
            f"{r['keyword_score']:>6.3f} "
            f"{r['entity_score']:>6.3f} "
            f"{r['forbidden_score']:>6.3f} "
            f"{r['length_score']:>5.1f} "
            f"{r['latency_score']:>6.3f} "
            f"{r['repetition_score']:>5.3f}"
        )

    print("\n" + "=" * 72)
    gate_status = "PASSED" if report.get("gate_passed") else "FAILED"
    print(f"  Gate ({report.get('required_pass_rate', PASS_RATE_GATE):.0%} pass rate + category floors): {gate_status}")
    print("=" * 72 + "\n")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    evaluator = ResponseEvaluator()
    responses = []

    print(f"Running evaluation against {API_URL} ...")
    print(f"Evaluating {len(evaluator.queries)} queries\n")

    for qid, spec in evaluator.queries.items():
        query_text = spec["query"]
        logger.info("Sending query %s: %r", qid, query_text)

        if qid == "hunt_hint":
            sender = HUNT_HINT_SENDER
            send_query("HUNT", sender=sender)
        elif qid == "hunt_clue_locked":
            sender = HUNT_LOCKED_SENDER
            send_query("HUNT", sender=sender)
        else:
            sender = HUNT_SENDER if qid.startswith("hunt_") else None
        response_text, latency_ms = send_query(query_text, sender=sender)

        if not response_text:
            logger.warning("Empty response for %s", qid)

        responses.append({
            "query_id": qid,
            "query": query_text,
            "response": response_text,
            "latency_ms": latency_ms,
        })
        print(f"  [{qid}] {latency_ms:7.0f}ms | {response_text[:60]}...")

    report = evaluator.run_evaluation(responses)
    required_pass_rate = float(evaluator.gate.get("min_pass_rate", PASS_RATE_GATE))
    category_requirements = evaluator.gate.get("category_min_pass_rate") or {}
    failed_categories = {
        category: {
            "required": float(required),
            "observed": report.get("category_pass_rates", {}).get(category, 0.0),
        }
        for category, required in category_requirements.items()
        if report.get("category_pass_rates", {}).get(category, 0.0) < float(required)
    }
    report["required_pass_rate"] = required_pass_rate
    report["failed_category_gates"] = failed_categories
    report["gate_passed"] = (
        report["pass_rate"] >= required_pass_rate and not failed_categories
    )

    # Write results to JSON
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as fh:
        json.dump(report, fh, indent=2)
    logger.info("Results written to %s", RESULTS_FILE)

    print_report(report)

    if report["gate_passed"]:
        print(f"Results saved to {RESULTS_FILE}")
        return 0
    else:
        print(f"Results saved to {RESULTS_FILE}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
