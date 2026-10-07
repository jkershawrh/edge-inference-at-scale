#!/usr/bin/env python3
"""Plan deterministic corpus coverage work from classified local records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.coverage import CoveragePlanError, plan_mission_coverage
from corpus_factory.validator import load_json


def _records(path: str, wrapper: str):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get(wrapper)
    if not isinstance(value, list):
        raise ValueError("{0} must be a JSON list or a '{0}' wrapper".format(wrapper))
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-profile", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--classifications", required=True)
    parser.add_argument("--documents")
    parser.add_argument("--as-of", required=True, help="planning time in ISO-8601")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    try:
        report = plan_mission_coverage(
            load_json(args.mission_profile),
            load_json(args.registry),
            _records(args.classifications, "classifications"),
            as_of=args.as_of,
            documents=_records(args.documents, "documents") if args.documents else (),
        )
    except (CoveragePlanError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print("coverage planning failed: {0}".format(exc), file=sys.stderr)
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["decision"] == "COVERED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
