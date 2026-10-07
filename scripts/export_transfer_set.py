#!/usr/bin/env python3
"""Create an immutable plaintext-lab or site-encrypted disconnected transfer set."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.distribution import (
    DistributionError,
    EncryptionRecipient,
    REQUIRED_ARTIFACTS,
    export_encrypted_transfer_set,
    export_transfer_set,
)


def _load_private_signer(path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("transfer signing key must be Ed25519")
    return key.sign


def _load_recipient(path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, X25519PublicKey):
        raise ValueError("recipient public key must be X25519")
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    for name in REQUIRED_ARTIFACTS:
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--classification", required=True)
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--recipient-public-key", type=Path)
    parser.add_argument("--recipient-key-id")
    parser.add_argument("--allow-unsigned-lab", action="store_true")
    parser.add_argument("--allow-unencrypted-lab", action="store_true")
    args = parser.parse_args()
    if not args.signing_key and not args.allow_unsigned_lab:
        parser.error("--signing-key is required unless --allow-unsigned-lab is explicit")
    if bool(args.recipient_public_key) != bool(args.recipient_key_id):
        parser.error("--recipient-public-key and --recipient-key-id must be used together")
    artifacts = {
        name: getattr(args, name)
        for name in REQUIRED_ARTIFACTS
    }
    try:
        signer = _load_private_signer(args.signing_key) if args.signing_key else None
        common = dict(
            output=args.output,
            artifacts=artifacts,
            media_id=args.media_id,
            event_id=args.event_id,
            site_id=args.site_id,
            sequence=args.sequence,
            classification=args.classification,
            manifest_signer=signer,
        )
        if args.recipient_public_key:
            export_encrypted_transfer_set(
                **common,
                recipient=EncryptionRecipient(
                    args.site_id,
                    args.recipient_key_id,
                    _load_recipient(args.recipient_public_key),
                ),
            )
            encrypted = True
        else:
            export_transfer_set(
                **common, allow_unencrypted_lab=args.allow_unencrypted_lab
            )
            encrypted = False
    except (DistributionError, OSError, ValueError) as exc:
        print("transfer export failed: {0}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "created": True,
        "encrypted": encrypted,
        "output": str(args.output.resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
