"""Fail-closed composition for the standalone Lil EVY activation operator.

Configuration is supplied explicitly by the caller.  This module never reads,
logs, serializes, or returns process environment values.
"""
from __future__ import annotations

import base64
import binascii
import re
import stat
from pathlib import Path
from typing import Any, Literal, Mapping

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .activation import CorpusActivationManager
from .activation_contracts import Digest
from .activation_control import ActivationControl, ActivationControlConfigurationError
from .activation_operator import create_activation_operator_app
from .receipt_signing import Ed25519ReceiptSigner, ReceiptSigningConfigurationError


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_RUNTIME_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_TOKEN = re.compile(r"^[\x21-\x7e]{32,512}$")
_MAX_KEY_BYTES = 16 * 1024


class ActivationBootstrapConfigurationError(RuntimeError):
    """Bounded startup failure safe to report without configuration values."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class ActivationOperatorSettings(BaseModel):
    """Complete explicit configuration required to construct the operator."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    activation_root: Path
    intake_root: Path
    trusted_corpus_public_key_path: Path
    receipt_private_key_path: Path
    receipt_key_id: str
    bearer_credential: SecretStr
    node_id: str
    cluster_id: str
    site_id: str
    expected_event_id: str
    expected_version: str
    policy_digest: Digest
    runtime_version: str
    chunker_digest: Digest
    model_digest: Digest
    embedding_model_digest: Digest
    time_confidence: Literal["trusted", "degraded", "unknown"]
    max_request_bytes: int = Field(ge=256, le=64 * 1024, strict=True)

    @field_validator(
        "receipt_key_id",
        "node_id",
        "cluster_id",
        "site_id",
        "expected_event_id",
        "expected_version",
    )
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("identity is invalid")
        return value

    @field_validator("runtime_version")
    @classmethod
    def validate_runtime_version(cls, value: str) -> str:
        if not _RUNTIME_VERSION.fullmatch(value):
            raise ValueError("runtime version is invalid")
        return value

    @field_validator("bearer_credential")
    @classmethod
    def validate_bearer_credential(cls, value: SecretStr) -> SecretStr:
        if not _TOKEN.fullmatch(value.get_secret_value()):
            raise ValueError("bearer credential is invalid")
        return value

    @model_validator(mode="after")
    def validate_filesystem_boundary(self) -> "ActivationOperatorSettings":
        roots = (self.activation_root, self.intake_root)
        for root in roots:
            if (not root.is_absolute() or not root.exists() or not root.is_dir()
                    or root.is_symlink()):
                raise ValueError("operator roots must be existing absolute non-symlink directories")

        activation = self.activation_root.resolve(strict=True)
        intake = self.intake_root.resolve(strict=True)
        if activation == intake or _is_relative_to(activation, intake) or _is_relative_to(intake, activation):
            raise ValueError("operator roots must be separate and non-overlapping")

        resolved_keys = []
        for key_path in (self.trusted_corpus_public_key_path, self.receipt_private_key_path):
            resolved_key = _validate_projected_key_path(key_path)
            resolved_keys.append(resolved_key)
            size = resolved_key.stat().st_size
            if size < 1 or size > _MAX_KEY_BYTES:
                raise ValueError("operator key size is invalid")

        if resolved_keys[0] == resolved_keys[1]:
            raise ValueError("trust and receipt keys must be distinct")
        private_mode = stat.S_IMODE(resolved_keys[1].stat().st_mode)
        allowed_private_mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
        if (private_mode & ~allowed_private_mode) or not (
            private_mode & (stat.S_IRUSR | stat.S_IRGRP)
        ):
            raise ValueError("receipt private key permissions are too broad")
        return self


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_projected_key_path(path: Path) -> Path:
    """Accept a regular key or one final projected-volume symlink.

    The configured parent and its ancestors must be real directories.  A final
    symlink may use a projected-volume indirection, but its resolved regular
    file must remain inside that configured parent directory.
    """

    if not path.is_absolute():
        raise ValueError("operator key paths must be absolute")
    parent = path.parent
    for directory in (parent, *parent.parents):
        if directory.is_symlink():
            raise ValueError("operator key directory chain must not contain symlinks")
    if not parent.exists() or not parent.is_dir():
        raise ValueError("operator key mount directory is invalid")

    mount = parent.resolve(strict=True)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("operator key file is missing or invalid") from exc
    if not resolved.is_file() or not _is_relative_to(resolved, mount):
        raise ValueError("projected operator key must resolve within its mount directory")
    return resolved


def _validate_corpus_public_key(path: Path) -> None:
    try:
        key_bytes = path.read_bytes()
        try:
            public_key = serialization.load_pem_public_key(key_bytes)
        except ValueError:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(key_bytes.strip(), validate=True)
            )
    except (OSError, ValueError, TypeError, binascii.Error, UnsupportedAlgorithm) as exc:
        raise ActivationBootstrapConfigurationError(
            "INVALID_TRUST_KEY", "The corpus trust key is invalid."
        ) from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise ActivationBootstrapConfigurationError(
            "INVALID_TRUST_KEY", "The corpus trust key must be Ed25519."
        )


def create_configured_activation_operator(
    values: ActivationOperatorSettings | Mapping[str, Any],
) -> FastAPI:
    """Validate all dependencies and return a fully composed operator app."""

    try:
        settings = (
            values
            if isinstance(values, ActivationOperatorSettings)
            else ActivationOperatorSettings.model_validate(values)
        )
    except (ValidationError, TypeError, ValueError, OSError) as exc:
        raise ActivationBootstrapConfigurationError(
            "INVALID_SETTINGS", "Activation operator configuration is invalid."
        ) from exc

    _validate_corpus_public_key(settings.trusted_corpus_public_key_path)

    try:
        signer = Ed25519ReceiptSigner(
            settings.receipt_private_key_path,
            key_id=settings.receipt_key_id,
        )
        manager = CorpusActivationManager(
            settings.activation_root,
            node_id=settings.node_id,
            cluster_id=settings.cluster_id,
            site_id=settings.site_id,
            trusted_public_key_path=settings.trusted_corpus_public_key_path,
            expected_event_id=settings.expected_event_id,
            expected_version=settings.expected_version,
            policy_digest=settings.policy_digest,
            runtime_identity={
                "version": settings.runtime_version,
                "chunker_digest": settings.chunker_digest,
                "model_digest": settings.model_digest,
                "embedding_model_digest": settings.embedding_model_digest,
            },
            time_confidence=settings.time_confidence,
            receipt_signer=signer,
        )
        control = ActivationControl(
            manager,
            bearer_credential=settings.bearer_credential.get_secret_value(),
            intake_root=settings.intake_root,
        )
        return create_activation_operator_app(
            control,
            max_request_bytes=settings.max_request_bytes,
        )
    except ReceiptSigningConfigurationError as exc:
        raise ActivationBootstrapConfigurationError(
            "INVALID_RECEIPT_KEY", "The receipt signing key is invalid."
        ) from exc
    except ActivationControlConfigurationError as exc:
        raise ActivationBootstrapConfigurationError(
            "INVALID_CONTROL_CONFIGURATION", "Activation control configuration is invalid."
        ) from exc
    except Exception as exc:
        raise ActivationBootstrapConfigurationError(
            "COMPONENT_CONFIGURATION_FAILED", "Activation operator initialization failed."
        ) from exc
