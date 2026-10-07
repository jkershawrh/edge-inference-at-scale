"""Tests for the node-held activation receipt signer."""
import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.services.rag_service.receipt_signing import (
    Ed25519ReceiptSigner,
    ReceiptSigningConfigurationError,
)


def _write_ed25519_key(path: Path) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


def test_signs_canonical_receipt_with_node_key(tmp_path: Path) -> None:
    path = tmp_path / "device-key.pem"
    private_key = _write_ed25519_key(path)
    signer = Ed25519ReceiptSigner(path, key_id="lil-evy-node-001")

    signed = signer(b'{"record_type":"activation_receipt"}')

    assert signed["key_id"] == "lil-evy-node-001"
    assert signed["algorithm"] == "Ed25519"
    private_key.public_key().verify(
        base64.b64decode(signed["value"]),
        b'{"record_type":"activation_receipt"}',
    )


@pytest.mark.parametrize("key_id", ["", "A-node", "ab", "node key", "../node"])
def test_rejects_invalid_key_identity(tmp_path: Path, key_id: str) -> None:
    path = tmp_path / "device-key.pem"
    _write_ed25519_key(path)

    with pytest.raises(ReceiptSigningConfigurationError, match="key ID"):
        Ed25519ReceiptSigner(path, key_id=key_id)


def test_rejects_missing_empty_or_oversized_key(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pem"
    with pytest.raises(ReceiptSigningConfigurationError, match="missing"):
        Ed25519ReceiptSigner(missing, key_id="node-001")

    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    with pytest.raises(ReceiptSigningConfigurationError, match="size"):
        Ed25519ReceiptSigner(empty, key_id="node-001")

    oversized = tmp_path / "oversized.pem"
    oversized.write_bytes(b"x" * (16 * 1024 + 1))
    with pytest.raises(ReceiptSigningConfigurationError, match="size"):
        Ed25519ReceiptSigner(oversized, key_id="node-001")


def test_rejects_non_ed25519_private_key(tmp_path: Path) -> None:
    path = tmp_path / "rsa-key.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    with pytest.raises(ReceiptSigningConfigurationError, match="must be Ed25519"):
        Ed25519ReceiptSigner(path, key_id="node-001")


def test_rejects_encrypted_or_malformed_key(tmp_path: Path) -> None:
    encrypted = tmp_path / "encrypted.pem"
    key = Ed25519PrivateKey.generate()
    encrypted.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"secret"),
        )
    )
    with pytest.raises(ReceiptSigningConfigurationError, match="invalid"):
        Ed25519ReceiptSigner(encrypted, key_id="node-001")

    malformed = tmp_path / "malformed.pem"
    malformed.write_text("not a private key", encoding="utf-8")
    with pytest.raises(ReceiptSigningConfigurationError, match="invalid"):
        Ed25519ReceiptSigner(malformed, key_id="node-001")


def test_rejects_empty_or_non_bytes_payload(tmp_path: Path) -> None:
    path = tmp_path / "device-key.pem"
    _write_ed25519_key(path)
    signer = Ed25519ReceiptSigner(path, key_id="node-001")

    for value in (b"", "text", None):
        with pytest.raises(ValueError, match="non-empty bytes"):
            signer(value)  # type: ignore[arg-type]
