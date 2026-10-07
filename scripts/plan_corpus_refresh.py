#!/usr/bin/env python3
"""Plan which registered Big EVY sources are due for acquisition."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.refresh import RefreshPlanError, plan_registry_refresh
from corpus_factory.validator import ContractValidationError, load_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--event-policy", required=True)
    parser.add_argument("--previous-source-record", action="append", default=[])
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        plan = plan_registry_refresh(
            load_json(args.registry),
            load_json(args.event_policy),
            [load_json(path) for path in args.previous_source_record],
            as_of=args.as_of,
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise FileExistsError("immutable output already exists: {0}".format(output))
        payload = (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.link(temporary, output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (ContractValidationError, RefreshPlanError, FileExistsError, OSError, ValueError) as exc:
        print("refresh planning failed: {0}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps({"plan_id": plan["plan_id"], "due_source_ids": plan["due_source_ids"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
