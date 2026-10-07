#!/usr/bin/env python3
"""Prepare exact evaluation-attestation bytes for an external Ed25519 signer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus_factory.evaluation_attestation import (
    EvaluationAttestationError,
    load_ed25519_public_key,
    load_json_object,
    prepare_attestation_statement,
    signing_payload,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promotion-report", required=True)
    parser.add_argument("--approvals", required=True, help="JSON array or approvals wrapper")
    parser.add_argument("--evaluation-executor-identity", required=True)
    parser.add_argument("--valid-from", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--revocation-generation", required=True, type=int)
    parser.add_argument("--revocation-checked-at", required=True)
    parser.add_argument("--signer-identity", required=True)
    parser.add_argument("--signed-at", required=True)
    parser.add_argument("--public-key", required=True, help="Ed25519 public key PEM")
    parser.add_argument("--key-valid-from", required=True)
    parser.add_argument("--key-expires-at", required=True)
    parser.add_argument("--output", required=True, help="unsigned statement JSON")
    parser.add_argument("--payload-output", required=True, help="exact bytes to sign")
    args = parser.parse_args(argv)

    try:
        approvals_value = json.loads(Path(args.approvals).read_text(encoding="utf-8"))
        if isinstance(approvals_value, dict):
            approvals_value = approvals_value.get("approvals")
        statement = prepare_attestation_statement(
            load_json_object(args.promotion_report),
            approvals_value,
            evaluation_executor_identity=args.evaluation_executor_identity,
            valid_from=args.valid_from,
            expires_at=args.expires_at,
            revocation_generation=args.revocation_generation,
            revocation_checked_at=args.revocation_checked_at,
            signer_identity=args.signer_identity,
            signed_at=args.signed_at,
            public_key=load_ed25519_public_key(Path(args.public_key).read_bytes()),
            key_valid_from=args.key_valid_from,
            key_expires_at=args.key_expires_at,
        )
        Path(args.output).write_text(
            json.dumps(statement, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        Path(args.payload_output).write_bytes(signing_payload(statement))
    except (OSError, UnicodeError, json.JSONDecodeError, EvaluationAttestationError) as exc:
        print(f"Evaluation attestation preparation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
