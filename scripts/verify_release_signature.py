#!/usr/bin/env python3
"""Offline-verify a protected release signature using a trusted public key."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from corpus_factory.release_signing import ReleaseSigningError, verify_release_signature
from corpus_factory.validator import load_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--authorization", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument(
        "--manifest-signature-output",
        help="after verification, write the Lil EVY-compatible base64 manifest.sig artifact",
    )
    args = parser.parse_args(argv)
    try:
        key = serialization.load_pem_public_key(Path(args.public_key).read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ReleaseSigningError("trusted public key must be Ed25519")
        record = load_json(args.signature)
        verify_release_signature(Path(args.candidate).read_bytes(), load_json(args.authorization), record, key)
        if args.manifest_signature_output:
            Path(args.manifest_signature_output).write_text(
                record["manifest_signature"]["value"] + "\n", encoding="ascii"
            )
    except (OSError, ValueError, ReleaseSigningError) as exc:
        print(f"Release signature verification failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"verified": True, "signature_id": record["signature_id"], "key_id": record["signing_key"]["key_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
