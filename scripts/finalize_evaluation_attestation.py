#!/usr/bin/env python3
"""Attach and verify an externally produced evaluation-attestation signature."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus_factory.evaluation_attestation import (
    EvaluationAttestationError,
    finalize_attestation,
    load_ed25519_public_key,
    load_json_object,
)


def _signature_bytes(path: str) -> bytes:
    value = Path(path).read_bytes()
    if len(value) == 64:
        return value
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EvaluationAttestationError("signature must be raw Ed25519 or base64") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--statement", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        record = finalize_attestation(
            load_json_object(args.statement),
            _signature_bytes(args.signature),
            load_ed25519_public_key(Path(args.public_key).read_bytes()),
        )
        Path(args.output).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, EvaluationAttestationError) as exc:
        print(f"Evaluation attestation finalization failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
