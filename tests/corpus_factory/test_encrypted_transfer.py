import json
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

from corpus_factory.distribution import (
    DistributionError,
    EncryptionRecipient,
    REQUIRED_ARTIFACTS,
    decrypt_transfer_set,
    export_encrypted_transfer_set,
    export_transfer_set,
    verify_transfer_set,
)


def _artifacts(tmp_path: Path):
    tmp_path.mkdir(parents=True)
    result = {}
    for position, name in enumerate(REQUIRED_ARTIFACTS):
        path = tmp_path / name
        path.write_bytes(("private-%s-%d" % (name, position)).encode())
        result[name] = path
    return result


def _encrypted(tmp_path: Path, private_key=None):
    private_key = private_key or X25519PrivateKey.generate()
    source = _artifacts(tmp_path / "source")
    transfer = export_encrypted_transfer_set(
        tmp_path / "transfer",
        source,
        media_id="media-secure-001",
        event_id="flood-response-2026",
        site_id="site-alpha",
        sequence=8,
        classification="restricted",
        recipient=EncryptionRecipient(
            "site-alpha", "site-alpha-key-2026", private_key.public_key()
        ),
        manifest_signer=lambda payload: b"signed:" + payload[:12],
    )
    return private_key, source, transfer


def _verified(transfer: Path, **overrides):
    arguments = dict(
        expected_event_id="flood-response-2026",
        expected_site_id="site-alpha",
        allowed_classifications={"restricted"},
        signature_verifier=lambda payload, signature: signature == b"signed:" + payload[:12],
    )
    arguments.update(overrides)
    return verify_transfer_set(transfer, **arguments)


def test_encrypted_transfer_contains_only_ciphertext_and_decrypts_after_verification(
    tmp_path: Path,
) -> None:
    private_key, source, transfer = _encrypted(tmp_path)
    verified = _verified(transfer)

    blobs = [p.read_bytes() for p in (transfer / "blobs" / "sha256").iterdir()]
    for path in source.values():
        assert path.read_bytes() not in blobs
    assert "content_key" not in json.dumps(verified.manifest)

    output = decrypt_transfer_set(
        verified,
        tmp_path / "staged",
        site_id="site-alpha",
        recipient_key_id="site-alpha-key-2026",
        private_key=private_key,
    )
    for name, path in source.items():
        assert (output / "artifacts" / name).read_bytes() == path.read_bytes()


def test_tampered_ciphertext_is_rejected_before_decryption(tmp_path: Path) -> None:
    private_key, _source, transfer = _encrypted(tmp_path)
    manifest = _manifest(transfer)
    cipher = transfer / manifest["artifacts"][0]["path"]
    cipher.write_bytes(cipher.read_bytes()[:-1] + b"X")

    with pytest.raises(DistributionError, match="integrity"):
        _verified(transfer)


def test_wrong_site_replay_and_wrong_private_key_fail_closed(tmp_path: Path) -> None:
    _private_key, _source, transfer = _encrypted(tmp_path)
    with pytest.raises(DistributionError, match="scope"):
        _verified(transfer, expected_site_id="site-bravo")
    with pytest.raises(DistributionError, match="sequence"):
        _verified(transfer, sequence_floor=8)
    verified = _verified(transfer)
    with pytest.raises(DistributionError, match="unwrap"):
        decrypt_transfer_set(
            verified,
            tmp_path / "wrong",
            site_id="site-alpha",
            recipient_key_id="site-alpha-key-2026",
            private_key=X25519PrivateKey.generate(),
        )


def test_recipient_must_match_transfer_scope(tmp_path: Path) -> None:
    key = X25519PrivateKey.generate()
    with pytest.raises(DistributionError, match="recipient"):
        export_encrypted_transfer_set(
            tmp_path / "transfer",
            _artifacts(tmp_path / "source"),
            media_id="media-secure-001",
            event_id="flood-response-2026",
            site_id="site-alpha",
            sequence=8,
            classification="restricted",
            recipient=EncryptionRecipient("site-bravo", "site-bravo-key", key.public_key()),
        )


def test_unencrypted_restricted_compatibility_is_explicit(tmp_path: Path) -> None:
    arguments = dict(
        output=tmp_path / "plain",
        artifacts=_artifacts(tmp_path / "source"),
        media_id="media-plain-001",
        event_id="flood-response-2026",
        site_id="site-alpha",
        sequence=1,
        classification="restricted",
    )
    with pytest.raises(DistributionError, match="explicit lab"):
        export_transfer_set(**arguments)
    export_transfer_set(**arguments, allow_unencrypted_lab=True)


def test_verify_cli_decrypts_only_after_successful_verification(tmp_path: Path) -> None:
    private_key, _source, transfer = _encrypted(tmp_path)
    private_path = tmp_path / "site-key.pem"
    private_path.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    # The fixture signer is intentionally synthetic, so exercise the explicit
    # unsigned lab trust mode while retaining all envelope integrity checks.
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts" / "verify_transfer_set.py"),
        str(transfer),
        "--event-id", "flood-response-2026",
        "--site-id", "site-alpha",
        "--allow-classification", "restricted",
        "--allow-unsigned",
        "--decryption-key", str(private_path),
        "--recipient-key-id", "site-alpha-key-2026",
        "--decrypt-to", str(tmp_path / "cli-stage"),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cli-stage" / "artifacts" / "corpus").is_file()

    failed = subprocess.run(
        [value if value != "site-alpha" else "site-bravo" for value in command],
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    assert "scope" in failed.stderr


def _manifest(transfer: Path):
    index = json.loads((transfer / "index.json").read_text())
    digest = index["manifests"][0]["digest"].split(":", 1)[1]
    return json.loads((transfer / "blobs" / "sha256" / digest).read_text())
