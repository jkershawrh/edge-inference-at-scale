#!/usr/bin/env python3
"""Run bounded connected acquisition over approved registry sources."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus_factory.acquisition_controller import (
    MAX_SOURCES_PER_CYCLE,
    AcquisitionController,
    AcquisitionControllerError,
    ControllerPaths,
)
from corpus_factory.validator import load_json


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--event-policy", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--mode", choices=("one-shot", "reconcile"), default="reconcile")
    parser.add_argument("--source-id")
    parser.add_argument("--max-sources", type=int, default=32)
    parser.add_argument("--max-cycles", type=int, default=1)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--observed-at", help="fixed ISO-8601 time; allowed only for one cycle")
    args = parser.parse_args(argv)

    if not 1 <= args.max_sources <= MAX_SOURCES_PER_CYCLE:
        parser.error(f"--max-sources must be between 1 and {MAX_SOURCES_PER_CYCLE}")
    if not 1 <= args.max_cycles <= 10000:
        parser.error("--max-cycles must be between 1 and 10000")
    if not 1 <= args.interval_seconds <= 86400:
        parser.error("--interval-seconds must be between 1 and 86400")
    if args.mode == "one-shot" and not args.source_id:
        parser.error("--mode one-shot requires --source-id")
    if args.mode == "one-shot" and args.max_cycles != 1:
        parser.error("--mode one-shot requires --max-cycles 1")
    if args.observed_at and args.max_cycles != 1:
        parser.error("--observed-at is allowed only with --max-cycles 1")

    root = Path(args.state_root)
    controller = AcquisitionController(
        ControllerPaths(
            evidence_store=root / "evidence",
            state_file=root / "controller-state.json",
            audit_ledger=root / "audit" / "acquisition.jsonl",
            outcome_store=root / "outcomes",
        ),
        actor=args.actor,
        max_sources=args.max_sources,
    )
    try:
        registry = load_json(args.registry)
        policy = load_json(args.event_policy)
        final = None
        for cycle in range(args.max_cycles):
            final = controller.reconcile(
                registry,
                policy,
                observed_at=args.observed_at or _now(),
                source_ids=[args.source_id] if args.source_id else None,
            )
            rendered = json.dumps(final, sort_keys=True, ensure_ascii=False)
            print(rendered, flush=True)
            if final["decision"] != "PASS":
                return 1
            if cycle + 1 < args.max_cycles:
                time.sleep(args.interval_seconds)
        return 0
    except (AcquisitionControllerError, OSError, ValueError) as exc:
        print(f"Acquisition controller failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
