#!/usr/bin/env python3
"""Acquire one approved HTTPS source into the Big EVY evidence vault."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.acquisition import AcquisitionError, acquire_registry_source
from corpus_factory.validator import ContractValidationError, load_json, validate_instance


def _write_file(path: Path, value) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--event-policy", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--evidence-store", required=True)
    parser.add_argument("--observed-at", required=True, help="trusted ISO-8601 acquisition time")
    parser.add_argument("--previous-source-record")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    try:
        registry = load_json(args.registry)
        validate_instance(registry, "source_registry")
        event_policy = load_json(args.event_policy)
        validate_instance(event_policy, "event_policy")
        previous_digest = None
        if args.previous_source_record:
            previous = load_json(args.previous_source_record)
            validate_instance(previous, "source_record")
            if previous["source_id"] != args.source_id:
                raise ValueError("previous source record has a different source_id")
            previous_digest = previous["evidence"]["digest"]
        result = acquire_registry_source(
            registry,
            event_policy,
            args.source_id,
            evidence_store=Path(args.evidence_store),
            observed_at=args.observed_at,
            previous_digest=previous_digest,
        )
        output = Path(args.output_dir)
        if output.exists():
            raise FileExistsError("immutable output already exists: {0}".format(output))
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".{0}.".format(output.name), dir=output.parent))
        try:
            _write_file(staging / (args.source_id + ".source-record.json"), result.snapshot.source_record)
            _write_file(staging / (args.source_id + ".acquisition-report.json"), result.report)
            os.replace(staging, output)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    except (
        AcquisitionError,
        ContractValidationError,
        FileExistsError,
        OSError,
        UnicodeError,
        ValueError,
    ) as exc:
        print("source acquisition failed: {0}".format(exc), file=sys.stderr)
        return 2

    print(json.dumps({
        "source_id": args.source_id,
        "evidence_digest": result.snapshot.digest,
        "change": result.report["change"],
        "report_id": result.report["report_id"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
