#!/usr/bin/env python3
"""Append to or verify the Corpus Factory tamper-evident audit ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.audit import AuditLedgerError, append_audit_event, verify_audit_ledger


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("ledger")
    append = subparsers.add_parser("append")
    append.add_argument("ledger")
    append.add_argument("--event-type", required=True)
    append.add_argument("--occurred-at", required=True)
    append.add_argument("--actor", required=True)
    append.add_argument("--subject-id", required=True)
    append.add_argument("--payload-digest", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_audit_ledger(Path(args.ledger))
        else:
            result = append_audit_event(
                Path(args.ledger),
                event_type=args.event_type,
                occurred_at=args.occurred_at,
                actor=args.actor,
                subject_id=args.subject_id,
                payload_digest=args.payload_digest,
            )
    except (AuditLedgerError, OSError, ValueError) as exc:
        print("audit ledger operation failed: {0}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
