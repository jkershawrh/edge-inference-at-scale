#!/usr/bin/env python3
"""Run the governed synthetic disaster release rehearsal."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.disaster_rehearsal import RehearsalError, run_rehearsal


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/synthetic-disaster-rehearsal")
    )
    args = parser.parse_args(argv)
    try:
        summary = run_rehearsal(args.output)
    except (OSError, ValueError, RehearsalError) as exc:
        print(f"Synthetic disaster rehearsal failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "decision": summary["decision"],
        "summary": str((args.output / "rehearsal-summary.json").resolve()),
        "summary_digest": summary["summary_digest"],
        "hardware_cut": summary["hardware_cut"]["status"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
