"""Transport-neutral reconciliation of activation acceptance with live serving.

An activation receipt proves a durable activation transaction completed.  It
does not prove that the currently running RAG process loaded that release.  The
state machine below requires a post-restart status observation before declaring
the deployment verified.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Union

from .activation_contracts import (
    ActivationMode,
    ActivationReceiptResponse,
    ActivationResult,
    ActivationStatusResponse,
    ActivationStatusState,
)


class ReconciliationError(ValueError):
    """Reconciliation input or configuration is invalid."""


class ReconciliationState(str, Enum):
    PENDING = "pending"
    RESTARTING = "restarting"
    VERIFIED = "verified"
    FAILED = "failed"


class ReconciliationReason(str, Enum):
    RESTART_REQUIRED = "RESTART_REQUIRED"
    RESTART_IN_PROGRESS = "RESTART_IN_PROGRESS"
    AWAITING_TARGET_STATUS = "AWAITING_TARGET_STATUS"
    TARGET_SERVING_VERIFIED = "TARGET_SERVING_VERIFIED"
    TARGET_STATUS_REJECTED = "TARGET_STATUS_REJECTED"
    RESTART_ATTEMPTS_EXHAUSTED = "RESTART_ATTEMPTS_EXHAUSTED"
    RECONCILIATION_TIMEOUT = "RECONCILIATION_TIMEOUT"


@dataclass(frozen=True)
class DeploymentReconciliationResult:
    """Bounded control-plane result; contains no paths, content, or credentials."""

    state: ReconciliationState
    reason_code: ReconciliationReason
    target_digest: str
    target_sequence: int
    observed_digest: Optional[str]
    observed_sequence: Optional[int]
    restart_attempts: int
    maximum_attempts: int
    elapsed_seconds: int
    timeout_seconds: int

    @property
    def complete(self) -> bool:
        return self.state in {ReconciliationState.VERIFIED, ReconciliationState.FAILED}


class DeploymentReconciler:
    """Reconcile one accepted production activation with post-restart status."""

    def __init__(
        self,
        receipt: Union[ActivationReceiptResponse, Mapping[str, Any]],
        *,
        maximum_attempts: int = 3,
        timeout_seconds: int = 300,
    ) -> None:
        try:
            accepted = (
                receipt
                if isinstance(receipt, ActivationReceiptResponse)
                else ActivationReceiptResponse.model_validate(receipt)
            )
        except Exception as exc:
            raise ReconciliationError("activation receipt is invalid") from exc
        if (
            accepted.result is not ActivationResult.SUCCESS
            or accepted.activated is None
            or accepted.activated != accepted.desired
            or accepted.transition.to_state != "ACTIVE"
        ):
            raise ReconciliationError(
                "reconciliation requires an accepted production activation receipt"
            )
        if type(maximum_attempts) is not int or not 1 <= maximum_attempts <= 100:
            raise ReconciliationError("maximum attempts must be between 1 and 100")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
            raise ReconciliationError("timeout must be between 1 and 86400 seconds")

        self._target_digest = accepted.desired.digest
        self._target_sequence = accepted.desired.sequence
        self._maximum_attempts = maximum_attempts
        self._timeout_seconds = timeout_seconds
        self._terminal: Optional[DeploymentReconciliationResult] = None
        self._last_restart_attempts = 0
        self._last_elapsed_seconds = 0.0

    def evaluate(
        self,
        observed_status: Optional[
            Union[ActivationStatusResponse, Mapping[str, Any]]
        ],
        *,
        restart_attempts: int,
        elapsed_seconds: float,
        restart_in_progress: bool = False,
    ) -> DeploymentReconciliationResult:
        """Advance reconciliation using one bounded live-status observation."""
        if self._terminal is not None:
            return self._terminal
        if type(restart_attempts) is not int or restart_attempts < 0:
            raise ReconciliationError("restart attempts must be a non-negative integer")
        if (
            isinstance(elapsed_seconds, bool)
            or not isinstance(elapsed_seconds, (int, float))
            or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0
        ):
            raise ReconciliationError("elapsed seconds must be finite and non-negative")
        if type(restart_in_progress) is not bool:
            raise ReconciliationError("restart_in_progress must be a boolean")
        if restart_in_progress and restart_attempts < 1:
            raise ReconciliationError("an in-progress restart requires an attempt")
        status = self._status(observed_status)
        if (
            restart_attempts < self._last_restart_attempts
            or elapsed_seconds < self._last_elapsed_seconds
        ):
            raise ReconciliationError("reconciliation progress cannot move backward")
        self._last_restart_attempts = restart_attempts
        self._last_elapsed_seconds = float(elapsed_seconds)

        elapsed = min(int(elapsed_seconds), self._timeout_seconds)

        # A successful observation at the final allowed attempt or exactly at
        # the deadline may complete reconciliation.  Evidence first observed
        # beyond either bound is too late and must not revive the deployment.
        if elapsed_seconds > self._timeout_seconds:
            return self._finish(
                self._result(
                    ReconciliationState.FAILED,
                    ReconciliationReason.RECONCILIATION_TIMEOUT,
                    status,
                    restart_attempts,
                    elapsed,
                )
            )
        if restart_attempts > self._maximum_attempts:
            return self._finish(
                self._result(
                    ReconciliationState.FAILED,
                    ReconciliationReason.RESTART_ATTEMPTS_EXHAUSTED,
                    status,
                    restart_attempts,
                    elapsed,
                )
            )

        if restart_attempts >= 1 and status is not None and self._is_verified(status):
            return self._finish(
                self._result(
                    ReconciliationState.VERIFIED,
                    ReconciliationReason.TARGET_SERVING_VERIFIED,
                    status,
                    restart_attempts,
                    elapsed,
                )
            )

        if restart_attempts >= 1 and status is not None and self._is_target(status):
            return self._finish(
                self._result(
                    ReconciliationState.FAILED,
                    ReconciliationReason.TARGET_STATUS_REJECTED,
                    status,
                    restart_attempts,
                    elapsed,
                )
            )

        if elapsed_seconds >= self._timeout_seconds:
            return self._finish(
                self._result(
                    ReconciliationState.FAILED,
                    ReconciliationReason.RECONCILIATION_TIMEOUT,
                    status,
                    restart_attempts,
                    elapsed,
                )
            )
        if restart_attempts >= self._maximum_attempts and not restart_in_progress:
            return self._finish(
                self._result(
                    ReconciliationState.FAILED,
                    ReconciliationReason.RESTART_ATTEMPTS_EXHAUSTED,
                    status,
                    restart_attempts,
                    elapsed,
                )
            )
        if restart_attempts == 0:
            return self._result(
                ReconciliationState.PENDING,
                ReconciliationReason.RESTART_REQUIRED,
                status,
                restart_attempts,
                elapsed,
            )
        return self._result(
            ReconciliationState.RESTARTING,
            ReconciliationReason.RESTART_IN_PROGRESS
            if restart_in_progress
            else ReconciliationReason.AWAITING_TARGET_STATUS,
            status,
            restart_attempts,
            elapsed,
        )

    @staticmethod
    def _status(
        value: Optional[Union[ActivationStatusResponse, Mapping[str, Any]]],
    ) -> Optional[ActivationStatusResponse]:
        if value is None:
            return None
        try:
            return (
                value
                if isinstance(value, ActivationStatusResponse)
                else ActivationStatusResponse.model_validate(value)
            )
        except Exception as exc:
            raise ReconciliationError("observed activation status is invalid") from exc

    def _is_target(self, status: ActivationStatusResponse) -> bool:
        return (
            status.active_digest == self._target_digest
            and status.active_sequence == self._target_sequence
        )

    def _is_verified(self, status: ActivationStatusResponse) -> bool:
        return (
            self._is_target(status)
            and status.mode is ActivationMode.PRODUCTION
            and status.state is ActivationStatusState.ACTIVE
            and status.ready is True
            and status.reason_code == "READY"
        )

    def _result(
        self,
        state: ReconciliationState,
        reason: ReconciliationReason,
        status: Optional[ActivationStatusResponse],
        attempts: int,
        elapsed: int,
    ) -> DeploymentReconciliationResult:
        return DeploymentReconciliationResult(
            state=state,
            reason_code=reason,
            target_digest=self._target_digest,
            target_sequence=self._target_sequence,
            observed_digest=status.active_digest if status is not None else None,
            observed_sequence=status.active_sequence if status is not None else None,
            restart_attempts=min(attempts, self._maximum_attempts),
            maximum_attempts=self._maximum_attempts,
            elapsed_seconds=elapsed,
            timeout_seconds=self._timeout_seconds,
        )

    def _finish(
        self, result: DeploymentReconciliationResult
    ) -> DeploymentReconciliationResult:
        self._terminal = result
        return result
