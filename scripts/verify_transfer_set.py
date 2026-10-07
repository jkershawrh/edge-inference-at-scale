#!/usr/bin/env python3
"""Offline verification for a disconnected transfer set (no import or writes)."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.distribution import (
    DistributionError,
    decrypt_transfer_set,
    verify_transfer_set,
)


def _ed25519_verifier(public_key_path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = serialization.load_pem_public_key(public_key_path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("transfer public key must be Ed25519")

    def verify(payload: bytes, signature: bytes) -> bool:
        try:
            key.verify(signature, payload)
            return True
        except Exception:
            return False

    return verify


def _x25519_private_key(private_key_path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    if not isinstance(key, X25519PrivateKey):
        raise ValueError("transfer decryption key must be X25519")
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transfer_set", type=Path)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--allow-classification", action="append", required=True)
    parser.add_argument("--sequence-floor", type=int, default=0)
    parser.add_argument("--used-media-id", action="append", default=[])
    trust = parser.add_mutually_exclusive_group(required=True)
    trust.add_argument("--public-key", type=Path, help="trusted Ed25519 transfer public key")
    trust.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="lab-only integrity check without authenticity verification",
    )
    parser.add_argument(
        "--decrypt-to", type=Path,
        help="decrypt a verified encrypted transfer into a new restrictive staging directory",
    )
    parser.add_argument("--decryption-key", type=Path, help="site X25519 private key in PEM")
    parser.add_argument("--recipient-key-id", help="expected site encryption key ID")
    args = parser.parse_args()
    decryption_options = (args.decrypt_to, args.decryption_key, args.recipient_key_id)
    if any(decryption_options) and not all(decryption_options):
        parser.error("--decrypt-to, --decryption-key, and --recipient-key-id must be used together")
    try:
        verifier = _ed25519_verifier(args.public_key) if args.public_key else None
        verified = verify_transfer_set(
            args.transfer_set,
            expected_event_id=args.event_id,
            expected_site_id=args.site_id,
            allowed_classifications=set(args.allow_classification),
            sequence_floor=args.sequence_floor,
            used_media_ids=args.used_media_id,
            signature_verifier=verifier,
            require_signature=not args.allow_unsigned,
        )
        if args.decrypt_to:
            decrypt_transfer_set(
                verified,
                args.decrypt_to,
                site_id=args.site_id,
                recipient_key_id=args.recipient_key_id,
                private_key=_x25519_private_key(args.decryption_key),
            )
    except (DistributionError, OSError, ValueError) as exc:
        print("transfer verification failed: {0}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "verified": True,
        "manifest_digest": verified.manifest_digest,
        "media_id": verified.manifest["media_id"],
        "sequence": verified.manifest["sequence"],
        "decrypted_to": str(args.decrypt_to.resolve()) if args.decrypt_to else None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
