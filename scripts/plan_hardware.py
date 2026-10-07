#!/usr/bin/env python3
"""Create an attributable hardware sizing report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.hardware_sizing import HardwareSizingError, build_hardware_sizing_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        plan = json.loads(args.input.read_text(encoding="utf-8"))
        report = build_hardware_sizing_report(plan)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, HardwareSizingError) as exc:
        print(f"hardware sizing failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "qualification": report["qualification"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
