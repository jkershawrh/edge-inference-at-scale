#!/usr/bin/env python3
"""Build a machine-readable corpus promotion decision from evaluator evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus_factory.promotion import evaluate_promotion, load_json_document
from corpus_factory.governance import CONTRACT_PROFILES


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-validity", required=True)
    parser.add_argument("--retrieval", required=True)
    parser.add_argument("--grounded-answer", required=True)
    parser.add_argument("--edge-profiles", required=True)
    parser.add_argument("--suitability", required=True)
    parser.add_argument(
        "--contract-profile",
        required=True,
        choices=CONTRACT_PROFILES,
        help="governed-v1 requires coverage and package governance; legacy-v1 is compatibility-only",
    )
    parser.add_argument(
        "--coverage-report",
        help=(
            "optional versioned coverage report; when supplied, release-validity "
            "must contain its exact coverage_report_binding"
        ),
    )
    parser.add_argument("--output", help="write the report here instead of stdout")
    args = parser.parse_args(argv)

    try:
        report = evaluate_promotion(
            load_json_document(args.release_validity),
            load_json_document(args.retrieval),
            load_json_document(args.grounded_answer),
            load_json_document(args.edge_profiles),
            load_json_document(args.suitability),
            load_json_document(args.coverage_report) if args.coverage_report else None,
            contract_profile=args.contract_profile,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Promotion evaluation failed: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["decision"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
