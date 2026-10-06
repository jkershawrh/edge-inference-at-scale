"""Fail-closed bootstrap tests for the Lil EVY activation operator."""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from backend.services.rag_service.activation_bootstrap import (
    ActivationBootstrapConfigurationError,
    ActivationOperatorSettings,
    create_configured_activation_operator,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _write_keys(tmp_path: Path) -> tuple[Path, Path]:
    corpus_key = Ed25519PrivateKey.generate()
    public_path = tmp_path / "corpus-public.pem"
    public_path.write_bytes(
        corpus_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    receipt_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "receipt-private.pem"
    private_path.write_bytes(
        receipt_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    return public_path, private_path


def _values(tmp_path: Path) -> dict[str, object]:
    activation_root = tmp_path / "activation"
    intake_root = tmp_path / "intake"
    activation_root.mkdir(parents=True)
    intake_root.mkdir()
    public_path, private_path = _write_keys(tmp_path)
    return {
        "activation_root": activation_root,
        "intake_root": intake_root,
        "trusted_corpus_public_key_path": public_path,
        "receipt_private_key_path": private_path,
        "receipt_key_id": "node-001-receipts",
        "bearer_credential": "operator-token-with-at-least-32-bytes",
        "node_id": "node-001",
        "cluster_id": "cluster-001",
        "site_id": "site-001",
        "expected_event_id": "flood-2026",
        "expected_version": "1.0.0",
        "policy_digest": _digest("1"),
        "runtime_version": "2.0.0",
        "chunker_digest": _digest("2"),
        "model_digest": _digest("3"),
        "embedding_model_digest": _digest("4"),
        "time_confidence": "trusted",
        "max_request_bytes": 8192,
    }


def test_builds_complete_operator_from_explicit_settings(tmp_path: Path) -> None:
    app = create_configured_activation_operator(_values(tmp_path))

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "service": "lil-evy-activation-operator",
        "status": "ready",
        "version": "1",
    }


def test_settings_hide_bearer_credential_from_repr_and_dump(tmp_path: Path) -> None:
    settings = ActivationOperatorSettings.model_validate(_values(tmp_path))

    assert "operator-token" not in repr(settings)
    assert "operator-token" not in str(settings.model_dump())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node_id", "NO SPACES"),
        ("policy_digest", "sha256:bad"),
        ("runtime_version", ""),
        ("time_confidence", "probably"),
        ("bearer_credential", "short"),
        ("max_request_bytes", "8192"),
    ],
)
def test_invalid_settings_fail_with_one_bounded_error(
    tmp_path: Path, field: str, value: object
) -> None:
    values = _values(tmp_path)
    values[field] = value

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)

    assert raised.value.reason_code == "INVALID_SETTINGS"
    assert str(raised.value) == "Activation operator configuration is invalid."
    if str(value):
        assert str(value) not in str(raised.value)


def test_missing_required_setting_and_unknown_setting_fail_closed(tmp_path: Path) -> None:
    missing = _values(tmp_path)
    missing.pop("site_id")
    unknown = _values(tmp_path / "other")
    unknown["debug_dump_environment"] = True

    for values in (missing, unknown):
        with pytest.raises(ActivationBootstrapConfigurationError) as raised:
            create_configured_activation_operator(values)
        assert raised.value.reason_code == "INVALID_SETTINGS"


@pytest.mark.parametrize("field", ["activation_root", "intake_root"])
def test_roots_must_be_absolute_existing_and_non_symlink(
    tmp_path: Path, field: str
) -> None:
    values = _values(tmp_path)
    values[field] = Path("relative-root")

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)
    assert raised.value.reason_code == "INVALID_SETTINGS"


def test_overlapping_roots_are_rejected(tmp_path: Path) -> None:
    values = _values(tmp_path)
    nested = values["activation_root"] / "intake"  # type: ignore[operator]
    nested.mkdir()
    values["intake_root"] = nested

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)
    assert raised.value.reason_code == "INVALID_SETTINGS"


@pytest.mark.parametrize("mode", [0o660, 0o740, 0o644])
def test_private_key_permissions_reject_group_write_execute_or_other_access(
    tmp_path: Path, mode: int
) -> None:
    values = _values(tmp_path)
    private_key = values["receipt_private_key_path"]
    assert isinstance(private_key, Path)
    private_key.chmod(mode)

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)
    assert raised.value.reason_code == "INVALID_SETTINGS"


def test_group_read_only_projected_secret_mode_is_accepted(tmp_path: Path) -> None:
    values = _values(tmp_path)
    private_key = values["receipt_private_key_path"]
    assert isinstance(private_key, Path)
    private_key.chmod(0o440)

    app = create_configured_activation_operator(values)

    assert TestClient(app).get("/health").status_code == 200


@pytest.mark.parametrize(
    "field",
    ["trusted_corpus_public_key_path", "receipt_private_key_path"],
)
def test_safe_in_mount_projected_key_symlink_is_accepted(
    tmp_path: Path, field: str
) -> None:
    values = _values(tmp_path)
    original = values[field]
    assert isinstance(original, Path)
    projected_directory = original.parent / ("projected-" + field)
    projected_directory.mkdir()
    target = projected_directory / "..2026-10-06" / "key.pem"
    target.parent.mkdir()
    target.write_bytes(original.read_bytes())
    if field == "receipt_private_key_path":
        target.chmod(0o440)
    data_link = projected_directory / "..data"
    data_link.symlink_to(target.parent.name, target_is_directory=True)
    link = projected_directory / "key.pem"
    link.symlink_to(Path("..data") / target.name)
    values[field] = link

    app = create_configured_activation_operator(values)

    assert TestClient(app).get("/health").status_code == 200


@pytest.mark.parametrize(
    "field",
    ["trusted_corpus_public_key_path", "receipt_private_key_path"],
)
def test_projected_key_symlink_escape_is_rejected(
    tmp_path: Path, field: str
) -> None:
    values = _values(tmp_path)
    original = values[field]
    assert isinstance(original, Path)
    mount = original.parent / ("projected-" + field)
    mount.mkdir()
    outside = tmp_path / ("outside-" + field + ".pem")
    outside.write_bytes(original.read_bytes())
    if field == "receipt_private_key_path":
        outside.chmod(0o440)
    link = mount / "key.pem"
    link.symlink_to(outside)
    values[field] = link

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)
    assert raised.value.reason_code == "INVALID_SETTINGS"


def test_key_path_with_symlinked_parent_is_rejected(tmp_path: Path) -> None:
    values = _values(tmp_path)
    original = values["trusted_corpus_public_key_path"]
    assert isinstance(original, Path)
    real_mount = tmp_path / "real-mount"
    real_mount.mkdir()
    target = real_mount / "key.pem"
    target.write_bytes(original.read_bytes())
    linked_mount = tmp_path / "linked-mount"
    linked_mount.symlink_to(real_mount, target_is_directory=True)
    values["trusted_corpus_public_key_path"] = linked_mount / "key.pem"

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)
    assert raised.value.reason_code == "INVALID_SETTINGS"


def test_invalid_corpus_trust_key_has_bounded_error(tmp_path: Path) -> None:
    values = _values(tmp_path)
    trust_key = values["trusted_corpus_public_key_path"]
    assert isinstance(trust_key, Path)
    trust_key.write_text("not a public key", encoding="utf-8")

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)
    assert raised.value.reason_code == "INVALID_TRUST_KEY"
    assert str(raised.value) == "The corpus trust key is invalid."
    assert str(trust_key) not in str(raised.value)


def test_non_ed25519_corpus_trust_key_is_rejected(tmp_path: Path) -> None:
    values = _values(tmp_path)
    trust_key = values["trusted_corpus_public_key_path"]
    assert isinstance(trust_key, Path)
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    trust_key.write_bytes(
        rsa_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)
    assert raised.value.reason_code == "INVALID_TRUST_KEY"


def test_invalid_receipt_private_key_has_bounded_error(tmp_path: Path) -> None:
    values = _values(tmp_path)
    private_key = values["receipt_private_key_path"]
    assert isinstance(private_key, Path)
    private_key.write_text("not a private key", encoding="utf-8")

    with pytest.raises(ActivationBootstrapConfigurationError) as raised:
        create_configured_activation_operator(values)
    assert raised.value.reason_code == "INVALID_RECEIPT_KEY"
    assert str(private_key) not in str(raised.value)
