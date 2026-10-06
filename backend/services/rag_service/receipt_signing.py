"""Device-held Ed25519 signing for bounded Lil EVY activation receipts."""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Dict

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_MAX_PRIVATE_KEY_BYTES = 16 * 1024


class ReceiptSigningConfigurationError(RuntimeError):
    """The node receipt signer cannot be safely configured."""


class Ed25519ReceiptSigner:
    """Load one node key and sign canonical receipt bytes without exposing it."""

    def __init__(self, private_key_path: Path, *, key_id: str) -> None:
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
            raise ReceiptSigningConfigurationError("receipt signing key ID is invalid")

        path = Path(private_key_path)
        if not path.is_file():
            raise ReceiptSigningConfigurationError("receipt signing private key is missing")
        try:
            size = path.stat().st_size
            if size < 1 or size > _MAX_PRIVATE_KEY_BYTES:
                raise ReceiptSigningConfigurationError(
                    "receipt signing private key has an invalid size"
                )
            private_bytes = path.read_bytes()
            loaded = serialization.load_pem_private_key(private_bytes, password=None)
        except ReceiptSigningConfigurationError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise ReceiptSigningConfigurationError(
                "receipt signing private key is invalid"
            ) from exc
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ReceiptSigningConfigurationError(
                "receipt signing private key must be Ed25519"
            )

        self._private_key = loaded
        self.key_id = key_id

    def __call__(self, canonical_receipt: bytes) -> Dict[str, str]:
        if not isinstance(canonical_receipt, bytes) or not canonical_receipt:
            raise ValueError("canonical receipt must be non-empty bytes")
        signature = self._private_key.sign(canonical_receipt)
        return {
            "key_id": self.key_id,
            "algorithm": "Ed25519",
            "value": base64.b64encode(signature).decode("ascii"),
        }
