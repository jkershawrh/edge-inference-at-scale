"""Tests for the activation operator's environment-to-ASGI adapter."""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from backend.services.rag_service.activation_bootstrap import (
    ActivationBootstrapConfigurationError,
)
from backend.services.rag_service.activation_operator_runtime import (
    create_activation_operator_from_environment,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _environment(tmp_path: Path) -> dict[str, str]:
    activation = tmp_path / "activation"
    intake = tmp_path / "intake"
    secrets = tmp_path / "secrets"
    activation.mkdir()
    intake.mkdir()
    secrets.mkdir()

    corpus_key = Ed25519PrivateKey.generate()
    public_key = secrets / "corpus-public.pem"
    public_key.write_bytes(
        corpus_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    receipt_key = secrets / "receipt-private.pem"
    receipt_key.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    receipt_key.chmod(0o600)
    token = secrets / "bearer-token"
    token.write_text("operator-token-with-at-least-32-bytes", encoding="ascii")

    return {
        "ACTIVATION_ROOT": str(activation),
        "ACTIVATION_INTAKE_ROOT": str(intake),
        "CORPUS_PUBLIC_KEY_PATH": str(public_key),
        "ACTIVATION_RECEIPT_PRIVATE_KEY_PATH": str(receipt_key),
        "ACTIVATION_RECEIPT_KEY_ID": "node-001-receipts",
        "ACTIVATION_BEARER_TOKEN_PATH": str(token),
        "NODE_ID": "node-001",
        "CLUSTER_ID": "cluster-001",
        "SITE_ID": "site-001",
        "CORPUS_EVENT_ID": "flood-2026",
        "CORPUS_VERSION": "1.0.0",
        "ACTIVATION_POLICY_DIGEST": _digest("1"),
        "ACTIVATION_RUNTIME_VERSION": "2.0.0",
        "ACTIVATION_CHUNKER_DIGEST": _digest("2"),
        "ACTIVATION_MODEL_DIGEST": _digest("3"),
        "ACTIVATION_EMBEDDING_MODEL_DIGEST": _digest("4"),
        "ACTIVATION_TIME_CONFIDENCE": "trusted",
        "ACTIVATION_MAX_REQUEST_BYTES": "8192",
    }


def test_builds_ready_operator_without_putting_token_in_environment(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)

    app = create_activation_operator_from_environment(environment)

    assert "operator-token" not in str(environment)
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda values: values.pop("SITE_ID"),
        lambda values: values.update(ACTIVATION_MAX_REQUEST_BYTES="many"),
        lambda values: values.update(ACTIVATION_BEARER_TOKEN_PATH="relative"),
    ],
)
def test_missing_or_invalid_environment_fails_with_bounded_error(
    tmp_path: Path, mutation
) -> None:
    environment = _environment(tmp_path)
    mutation(environment)

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_activation_operator_from_environment(environment)

    assert raised.value.reason_code == "INVALID_ENVIRONMENT"
    assert str(raised.value) == (
        "Activation operator environment configuration is invalid."
    )
    assert str(tmp_path) not in str(raised.value)


def test_token_file_must_be_ascii_and_bounded(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    token = Path(environment["ACTIVATION_BEARER_TOKEN_PATH"])

    for value in (b"short", b"x" * 513, b"x" * 31 + b"\xff"):
        token.write_bytes(value)
        with pytest.raises(ActivationBootstrapConfigurationError):
            create_activation_operator_from_environment(environment)


def test_projected_token_symlink_must_resolve_within_mount(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    token = Path(environment["ACTIVATION_BEARER_TOKEN_PATH"])
    actual = token.parent / "..data-token"
    token.rename(actual)
    token.symlink_to(actual.name)

    app = create_activation_operator_from_environment(environment)
    assert TestClient(app).get("/health").status_code == 200

    outside = tmp_path / "outside-token"
    outside.write_text("outside-token-with-at-least-thirty-two-bytes", encoding="ascii")
    token.unlink()
    token.symlink_to(outside)
    with pytest.raises(ActivationBootstrapConfigurationError):
        create_activation_operator_from_environment(environment)


def test_token_mount_parent_must_not_be_a_symlink(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    token = Path(environment["ACTIVATION_BEARER_TOKEN_PATH"])
    linked_mount = tmp_path / "linked-secrets"
    linked_mount.symlink_to(token.parent, target_is_directory=True)
    environment["ACTIVATION_BEARER_TOKEN_PATH"] = str(linked_mount / token.name)

    with pytest.raises(ActivationBootstrapConfigurationError):
        create_activation_operator_from_environment(environment)
