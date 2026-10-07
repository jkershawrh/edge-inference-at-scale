#!/usr/bin/env python3
"""Write bounded sourcing work from a mission profile and coverage report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.sourcing import SourcingPlanError, build_sourcing_work_items
from corpus_factory.validator import load_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-profile", required=True)
    parser.add_argument("--coverage-report", required=True)
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    try:
        plan = build_sourcing_work_items(
            load_json(args.mission_profile),
            load_json(args.coverage_report),
            max_items=args.max_items,
        )
    except (SourcingPlanError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print("sourcing planning failed: {0}".format(exc), file=sys.stderr)
        return 2

    rendered = json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")

    # Gaps are the expected input to this advisory stage, not an execution error.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
