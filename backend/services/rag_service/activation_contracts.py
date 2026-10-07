"""Strict operator API contracts for Lil EVY corpus activation.

These models define an internal control-plane boundary only.  They neither
authorize an operator nor activate, inspect, or resolve a package path.
"""
from __future__ import annotations

import posixpath
import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
ReasonCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]

_URI_OR_WINDOWS_PATH = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:|^[A-Za-z]:[\\/]")


class StrictContract(BaseModel):
    """Shared fail-closed behavior for the internal operator API."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class ActivationCandidateRequest(StrictContract):
    """A release already present in the server's local intake area."""

    digest: Digest
    sequence: PositiveInt
    package_path: str = Field(min_length=2, max_length=4096)

    @model_validator(mode="after")
    def validate_local_package_path(self) -> "ActivationCandidateRequest":
        value = self.package_path
        if ("\x00" in value or "\\" in value or _URI_OR_WINDOWS_PATH.match(value)
                or not value.startswith("/") or value.startswith("//")
                or value == "/" or posixpath.normpath(value) != value
                or any(part in {"", ".", ".."} for part in value.split("/")[1:])):
            raise ValueError(
                "package_path must be a normalized absolute server-local POSIX path"
            )
        return self


class ActivationMode(str, Enum):
    UNINITIALIZED = "uninitialized"
    PRODUCTION = "production"
    RECOVERY = "recovery"


class ActivationStatusState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    ACTIVE = "ACTIVE"
    RECOVERY = "RECOVERY"
    REJECTED = "REJECTED"


class ActivationStatusResponse(StrictContract):
    """Bounded view of the corpus that the node is prepared to serve."""

    active_digest: Optional[Digest]
    active_sequence: int = Field(ge=0)
    sequence_floor: int = Field(ge=0)
    mode: ActivationMode
    state: ActivationStatusState
    ready: bool
    reason_code: ReasonCode

    @model_validator(mode="after")
    def validate_coherent_status(self) -> "ActivationStatusResponse":
        if self.active_sequence > self.sequence_floor:
            raise ValueError("active_sequence cannot exceed sequence_floor")

        if self.active_digest is None:
            if not (
                self.active_sequence == 0
                and self.mode is ActivationMode.UNINITIALIZED
                and self.state is ActivationStatusState.UNINITIALIZED
                and not self.ready
                and self.reason_code == "NO_ACTIVE_CORPUS"
            ):
                raise ValueError("a node without an active corpus must be uninitialized")
            return self

        if self.active_sequence < 1 or self.mode is ActivationMode.UNINITIALIZED:
            raise ValueError("an active corpus requires a positive sequence and initialized mode")

        expected_state = (
            ActivationStatusState.RECOVERY
            if self.mode is ActivationMode.RECOVERY
            else ActivationStatusState.ACTIVE
        )
        if self.ready:
            expected_reason = "RECOVERY_ACTIVE" if self.mode is ActivationMode.RECOVERY else "READY"
            if self.state is not expected_state or self.reason_code != expected_reason:
                raise ValueError("ready status does not match activation mode")
        elif self.state is not ActivationStatusState.REJECTED:
            raise ValueError("a non-ready initialized node must report REJECTED state")
        return self


class ReleaseIdentityResponse(StrictContract):
    digest: Digest
    sequence: PositiveInt


class ActivationTransitionResponse(StrictContract):
    from_state: Annotated[str, Field(alias="from", pattern=r"^[A-Z][A-Z_]{2,31}$")]
    to_state: Annotated[str, Field(alias="to", pattern=r"^[A-Z][A-Z_]{2,31}$")]


class ActivationResult(str, Enum):
    SUCCESS = "success"
    REJECTED = "rejected"
    RECOVERED = "recovered"
    FAILED = "failed"


class ActivationReceiptResponse(StrictContract):
    """Safe, fixed-size receipt projection returned by an activation request."""

    receipt_id: UUID
    desired: ReleaseIdentityResponse
    activated: Optional[ReleaseIdentityResponse]
    previous_digest: Optional[Digest]
    transition: ActivationTransitionResponse
    result: ActivationResult
    reason_code: ReasonCode
    device_counter: PositiveInt
    created_at: datetime

    @model_validator(mode="after")
    def validate_coherent_result(self) -> "ActivationReceiptResponse":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")

        if self.result in {ActivationResult.SUCCESS, ActivationResult.RECOVERED}:
            if self.activated != self.desired:
                raise ValueError("successful receipt must activate the desired release")
            expected_to = "RECOVERY" if self.result is ActivationResult.RECOVERED else "ACTIVE"
            if self.transition.to_state != expected_to:
                raise ValueError("successful receipt has an inconsistent transition")
        elif self.transition.to_state != "REJECTED":
            raise ValueError("unsuccessful receipt must transition to REJECTED")
        return self


class ActivationAcceptedResponse(StrictContract):
    """Operator acknowledgement; activation does not mutate the live process."""

    receipt: ActivationReceiptResponse
    live_reload_performed: Literal[False] = False
    restart_or_reconciliation_required: Literal[True] = True

    @model_validator(mode="after")
    def require_success_receipt(self) -> "ActivationAcceptedResponse":
        if self.receipt.result is not ActivationResult.SUCCESS:
            raise ValueError("accepted activation must contain a success receipt")
        return self
