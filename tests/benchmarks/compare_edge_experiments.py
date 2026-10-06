#!/usr/bin/env python3
"""Compare controlled edge experiment reports after enforcing comparability."""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.benchmarks.edge_experiment import comparison_rows, load_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        rows = comparison_rows([load_report(path) for path in args.reports])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print("Comparison failed: {0}".format(exc))
        return 2
    result = {"schema_version": "1.0", "experiments": rows}
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
