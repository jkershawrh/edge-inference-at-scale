#!/usr/bin/env python3
"""Prepare, finalize, or offline-verify external corpus audit anchors."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.audit_anchor import (
    AuditAnchorError,
    finalize_receipt,
    load_ed25519_public_key,
    load_json_object,
    prepare_checkpoint,
    prepare_witness_statement,
    public_key_id,
    signing_payload,
    verify_anchor_chain,
)


def _signature_bytes(path: str) -> bytes:
    value = Path(path).read_bytes()
    if len(value) == 64:
        return value
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AuditAnchorError("signature must be raw Ed25519 or base64") from exc


def _revoked(path: str | None) -> list[str]:
    if path is None:
        return []
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AuditAnchorError("revoked keys must be a JSON string array")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--ledger", required=True)
    prepare.add_argument("--ledger-id", required=True)
    prepare.add_argument("--requester-identity", required=True)
    prepare.add_argument("--created-at", required=True)
    prepare.add_argument("--previous-receipt")
    prepare.add_argument("--previous-public-key")
    prepare.add_argument("--witness-identity", required=True)
    prepare.add_argument("--witness-organization", required=True)
    prepare.add_argument("--signed-at", required=True)
    prepare.add_argument("--witness-public-key", required=True)
    prepare.add_argument("--key-valid-from", required=True)
    prepare.add_argument("--key-expires-at", required=True)
    prepare.add_argument("--revocation-generation", required=True, type=int)
    prepare.add_argument("--revocation-checked-at", required=True)
    prepare.add_argument("--output", required=True, help="unsigned witness statement JSON")
    prepare.add_argument("--payload-output", required=True, help="exact bytes to sign")

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--statement", required=True)
    finalize.add_argument("--signature", required=True)
    finalize.add_argument("--witness-public-key", required=True)
    finalize.add_argument("--output", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--ledger", required=True)
    verify.add_argument("--receipt", action="append", required=True)
    verify.add_argument("--witness-public-key", action="append", required=True)
    verify.add_argument("--as-of", required=True)
    verify.add_argument("--revoked-keys")
    args = parser.parse_args(argv)

    try:
        if args.command == "prepare":
            if bool(args.previous_receipt) != bool(args.previous_public_key):
                raise AuditAnchorError(
                    "previous receipt and previous public key must be supplied together"
                )
            previous = (
                load_json_object(args.previous_receipt) if args.previous_receipt else None
            )
            previous_key = (
                load_ed25519_public_key(Path(args.previous_public_key).read_bytes())
                if args.previous_public_key
                else None
            )
            checkpoint = prepare_checkpoint(
                args.ledger,
                ledger_id=args.ledger_id,
                requester_identity=args.requester_identity,
                created_at=args.created_at,
                previous_receipt=previous,
                previous_public_key=previous_key,
            )
            key = load_ed25519_public_key(Path(args.witness_public_key).read_bytes())
            statement = prepare_witness_statement(
                checkpoint,
                witness_identity=args.witness_identity,
                witness_organization=args.witness_organization,
                signed_at=args.signed_at,
                public_key=key,
                key_valid_from=args.key_valid_from,
                key_expires_at=args.key_expires_at,
                revocation_generation=args.revocation_generation,
                revocation_checked_at=args.revocation_checked_at,
            )
            Path(args.output).write_text(
                json.dumps(statement, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            Path(args.payload_output).write_bytes(signing_payload(statement))
            result = {"checkpoint_id": checkpoint["checkpoint_id"], "key_id": public_key_id(key)}
        elif args.command == "finalize":
            key = load_ed25519_public_key(Path(args.witness_public_key).read_bytes())
            receipt = finalize_receipt(
                load_json_object(args.statement), _signature_bytes(args.signature), key
            )
            Path(args.output).write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            result = {"receipt_id": receipt["receipt_id"], "output": args.output}
        else:
            keys = [
                load_ed25519_public_key(Path(path).read_bytes())
                for path in args.witness_public_key
            ]
            result = verify_anchor_chain(
                args.ledger,
                [load_json_object(path) for path in args.receipt],
                {public_key_id(key): key for key in keys},
                as_of=args.as_of,
                revoked_key_ids=_revoked(args.revoked_keys),
            )
    except (OSError, UnicodeError, json.JSONDecodeError, AuditAnchorError) as exc:
        print(f"audit anchor operation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
