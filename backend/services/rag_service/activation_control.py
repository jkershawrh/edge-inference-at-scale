"""Authenticated, filesystem-confined control boundary for corpus activation.

This component is intentionally transport agnostic.  An HTTP layer may pass an
Authorization header and a validated request to it, but this module does not
create routes, read environment variables, or expose manager internals.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Optional

from .activation import ActivationError, CorpusActivationManager, ReleaseCandidate
from .activation_contracts import (
    ActivationCandidateRequest,
    ActivationReceiptResponse,
)


_BEARER_TOKEN = re.compile(r"^[\x21-\x7e]{32,512}$")


class ActivationControlConfigurationError(RuntimeError):
    """The activation control boundary cannot be operated safely."""


class ActivationAuthenticationError(PermissionError):
    """The presented operator credential was not accepted."""


class ActivationPathError(ValueError):
    """The candidate package is not a safe intake-root directory."""


class ActivationControl:
    """Authenticate and dispatch production activation requests.

    The configured credential is the token only, without the ``Bearer`` scheme.
    Candidate packages must already exist directly or recursively beneath the
    configured intake root.  Symlinks are rejected at every path component.
    """

    def __init__(
        self,
        manager: CorpusActivationManager,
        *,
        bearer_credential: Optional[str],
        intake_root: Path,
    ) -> None:
        if not isinstance(bearer_credential, str) or not _BEARER_TOKEN.fullmatch(
            bearer_credential
        ):
            raise ActivationControlConfigurationError(
                "a valid activation bearer credential is required"
            )

        root = Path(intake_root)
        if not root.is_absolute() or not root.exists() or not root.is_dir() or root.is_symlink():
            raise ActivationControlConfigurationError(
                "activation intake root must be an existing absolute non-symlink directory"
            )

        self._manager = manager
        self._expected_authorization_digest = hashlib.sha256(
            ("Bearer " + bearer_credential).encode("ascii")
        ).digest()
        self._intake_root = root.resolve(strict=True)

    def activate(
        self,
        request: ActivationCandidateRequest,
        authorization: Optional[str],
    ) -> ActivationReceiptResponse:
        """Authenticate, confine, activate, and return a bounded receipt.

        Expected activation rejections and failures still return their signed
        manager receipt as a bounded response.  No recovery operation is
        exposed by this component.
        """

        self._authenticate(authorization)
        package_path = self._confined_package_path(request.package_path)
        candidate = ReleaseCandidate(
            digest=request.digest,
            sequence=request.sequence,
            package_path=package_path,
        )
        try:
            receipt = self._manager.activate(candidate)
        except ActivationError as exc:
            receipt = exc.receipt
        return self._bounded_receipt(receipt)

    def _authenticate(self, authorization: Optional[str]) -> None:
        presented = authorization if isinstance(authorization, str) else ""
        presented_digest = hashlib.sha256(presented.encode("utf-8")).digest()
        if not hmac.compare_digest(presented_digest, self._expected_authorization_digest):
            raise ActivationAuthenticationError("activation credential was not accepted")

    def _confined_package_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        try:
            relative = candidate.relative_to(self._intake_root)
        except ValueError as exc:
            raise ActivationPathError("package_path is outside the activation intake root") from exc
        if not relative.parts:
            raise ActivationPathError("package_path must identify a package beneath the intake root")

        current = self._intake_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ActivationPathError("package_path must not contain symlinks")

        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, RuntimeError, OSError) as exc:
            raise ActivationPathError("package_path must identify an existing package directory") from exc
        try:
            resolved.relative_to(self._intake_root)
        except ValueError as exc:
            raise ActivationPathError("package_path escapes the activation intake root") from exc
        if not resolved.is_dir():
            raise ActivationPathError("package_path must identify an existing package directory")
        return resolved

    @staticmethod
    def _bounded_receipt(receipt: object) -> ActivationReceiptResponse:
        activated = None
        activated_digest = getattr(receipt, "activated_digest")
        activated_sequence = getattr(receipt, "activated_sequence")
        if activated_digest is not None:
            activated = {"digest": activated_digest, "sequence": activated_sequence}

        return ActivationReceiptResponse.model_validate(
            {
                "receipt_id": getattr(receipt, "receipt_id"),
                "desired": {
                    "digest": getattr(receipt, "desired_digest"),
                    "sequence": getattr(receipt, "desired_sequence"),
                },
                "activated": activated,
                "previous_digest": getattr(receipt, "previous_digest"),
                "transition": {
                    "from": getattr(receipt, "transition_from"),
                    "to": getattr(receipt, "transition_to"),
                },
                "result": getattr(receipt, "result"),
                "reason_code": getattr(receipt, "reason_code"),
                "device_counter": getattr(receipt, "device_counter"),
                "created_at": getattr(receipt, "created_at"),
            }
        )
