"""Minimal environment adapter for the activation-operator ASGI factory."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from fastapi import FastAPI

from .activation_bootstrap import (
    ActivationBootstrapConfigurationError,
    create_configured_activation_operator,
)


_REQUIRED_ENVIRONMENT = {
    "ACTIVATION_ROOT": "activation_root",
    "ACTIVATION_INTAKE_ROOT": "intake_root",
    "CORPUS_PUBLIC_KEY_PATH": "trusted_corpus_public_key_path",
    "ACTIVATION_RECEIPT_PRIVATE_KEY_PATH": "receipt_private_key_path",
    "ACTIVATION_RECEIPT_KEY_ID": "receipt_key_id",
    "NODE_ID": "node_id",
    "CLUSTER_ID": "cluster_id",
    "SITE_ID": "site_id",
    "CORPUS_EVENT_ID": "expected_event_id",
    "CORPUS_VERSION": "expected_version",
    "ACTIVATION_POLICY_DIGEST": "policy_digest",
    "ACTIVATION_RUNTIME_VERSION": "runtime_version",
    "ACTIVATION_CHUNKER_DIGEST": "chunker_digest",
    "ACTIVATION_MODEL_DIGEST": "model_digest",
    "ACTIVATION_EMBEDDING_MODEL_DIGEST": "embedding_model_digest",
    "ACTIVATION_TIME_CONFIDENCE": "time_confidence",
    "ACTIVATION_MAX_REQUEST_BYTES": "max_request_bytes",
}
_TOKEN_PATH_ENVIRONMENT = "ACTIVATION_BEARER_TOKEN_PATH"
_MAX_TOKEN_FILE_BYTES = 512


def _configuration_error() -> ActivationBootstrapConfigurationError:
    return ActivationBootstrapConfigurationError(
        "INVALID_ENVIRONMENT",
        "Activation operator environment configuration is invalid.",
    )


def _read_projected_token(path_value: str) -> str:
    path = Path(path_value)
    try:
        if not path.is_absolute():
            raise ValueError
        for directory in (path.parent, *path.parent.parents):
            if directory.is_symlink():
                raise ValueError
        if not path.is_file():
            raise ValueError
        parent = path.parent.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(parent)
        size = resolved.stat().st_size
        if not 32 <= size <= _MAX_TOKEN_FILE_BYTES:
            raise ValueError
        return resolved.read_bytes().decode("ascii")
    except (OSError, UnicodeDecodeError, ValueError):
        raise _configuration_error() from None


def create_activation_operator_from_environment(
    environment: Optional[Mapping[str, str]] = None,
) -> FastAPI:
    """Build the ASGI app from explicit variables and a file-mounted token."""
    source = os.environ if environment is None else environment
    try:
        values = {
            setting_name: source[environment_name]
            for environment_name, setting_name in _REQUIRED_ENVIRONMENT.items()
        }
        values["max_request_bytes"] = int(values["max_request_bytes"])
        values["bearer_credential"] = _read_projected_token(
            source[_TOKEN_PATH_ENVIRONMENT]
        )
    except (KeyError, TypeError, ValueError):
        raise _configuration_error() from None
    return create_configured_activation_operator(values)
