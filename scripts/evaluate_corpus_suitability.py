#!/usr/bin/env python3
"""Evaluate whether a corpus satisfies one event/deployment policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.suitability import SuitabilityInputError, evaluate_suitability


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _records(path: str, wrapper: str):
    value = _load(path)
    if isinstance(value, dict):
        value = value.get(wrapper)
    if not isinstance(value, list):
        raise ValueError("{0} must be a JSON list or '{1}' wrapper".format(path, wrapper))
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--sources", required=True)
    parser.add_argument("--documents", required=True)
    parser.add_argument("--evaluation-cases", required=True)
    parser.add_argument("--as-of", required=True, help="trusted evaluation time in ISO-8601")
    parser.add_argument(
        "--time-confidence",
        required=True,
        choices=("trusted", "anchored", "untrusted", "unknown"),
    )
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    try:
        policy = _load(args.policy)
        if not isinstance(policy, dict):
            raise ValueError("policy must be a JSON object")
        report = evaluate_suitability(
            policy,
            _records(args.sources, "sources"),
            _records(args.documents, "documents"),
            _records(args.evaluation_cases, "cases"),
            as_of=args.as_of,
            time_confidence=args.time_confidence,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, SuitabilityInputError) as exc:
        print("suitability evaluation failed: {0}".format(exc), file=sys.stderr)
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["decision"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
