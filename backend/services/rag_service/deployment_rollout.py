"""Crash-resumable, transport-neutral rollout control for Lil EVY corpora.

Adapters own HTTP, Kubernetes, or other transport details.  The controller
persists intent before side effects and requires an idempotency key at every
side-effect boundary so recovery can safely replay an interrupted operation.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, Protocol, Union
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .activation_contracts import (
    ActivationAcceptedResponse,
    ActivationCandidateRequest,
    ActivationStatusResponse,
    Digest,
    ReasonCode,
    ReleaseIdentityResponse,
    StrictContract,
)
from .deployment_reconciliation import DeploymentReconciler, ReconciliationState


class RolloutConfigurationError(ValueError):
    """Static controller configuration is invalid."""


class RolloutStateError(RuntimeError):
    """Persisted rollout state is missing, corrupt, or inconsistent."""


class RolloutPortError(RuntimeError):
    """An injected port failed without exposing its underlying details."""


class RolloutPhase(str, Enum):
    ACTIVATING = "activating"
    RESTARTING = "restarting"
    OBSERVING = "observing"
    VERIFIED = "verified"
    FAILED = "failed"


class RolloutReason(str, Enum):
    ACTIVATION_REQUIRED = "ACTIVATION_REQUIRED"
    ACTIVATION_ATTEMPTS_EXHAUSTED = "ACTIVATION_ATTEMPTS_EXHAUSTED"
    RESTART_REQUIRED = "RESTART_REQUIRED"
    RESTART_IN_PROGRESS = "RESTART_IN_PROGRESS"
    AWAITING_TARGET_STATUS = "AWAITING_TARGET_STATUS"
    TARGET_SERVING_VERIFIED = "TARGET_SERVING_VERIFIED"
    TARGET_STATUS_REJECTED = "TARGET_STATUS_REJECTED"
    RESTART_ATTEMPTS_EXHAUSTED = "RESTART_ATTEMPTS_EXHAUSTED"
    STATUS_POLLS_EXHAUSTED = "STATUS_POLLS_EXHAUSTED"
    ROLLOUT_TIMEOUT = "ROLLOUT_TIMEOUT"
    RECONCILIATION_TIMEOUT = "RECONCILIATION_TIMEOUT"


class RolloutConfiguration(StrictContract):
    maximum_activation_attempts: int = Field(default=3, ge=1, le=100)
    maximum_restart_attempts: int = Field(default=3, ge=1, le=100)
    maximum_status_polls: int = Field(default=60, ge=1, le=10000)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)


class PersistedRolloutState(StrictContract):
    """Bounded durable state.  It intentionally contains no local paths."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    rollout_id: UUID
    target: ReleaseIdentityResponse
    phase: RolloutPhase
    reason_code: RolloutReason
    receipt: Optional[ActivationAcceptedResponse] = None
    activation_attempts: int = Field(default=0, ge=0, le=100)
    restart_attempts: int = Field(default=0, ge=0, le=100)
    restart_acknowledged: bool = False
    status_polls: int = Field(default=0, ge=0, le=10000)
    observed_digest: Optional[Digest] = None
    observed_sequence: Optional[int] = Field(default=None, ge=0)
    started_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def coherent(self) -> "PersistedRolloutState":
        for value in (self.started_at, self.updated_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("rollout timestamps must include a timezone")
        if self.updated_at < self.started_at:
            raise ValueError("rollout timestamps cannot move backward")
        if self.receipt is None and self.phase not in {
            RolloutPhase.ACTIVATING,
            RolloutPhase.FAILED,
        }:
            raise ValueError("post-activation state requires a receipt")
        if (
            self.receipt is None
            and self.phase is RolloutPhase.FAILED
            and self.reason_code
            not in {
                RolloutReason.ACTIVATION_ATTEMPTS_EXHAUSTED,
                RolloutReason.ROLLOUT_TIMEOUT,
            }
        ):
            raise ValueError("pre-activation failure reason is invalid")
        if self.receipt is not None and self.receipt.receipt.desired != self.target:
            raise ValueError("receipt target does not match rollout target")
        if self.restart_acknowledged and self.restart_attempts < 1:
            raise ValueError("restart acknowledgement requires an attempt")
        if self.phase is RolloutPhase.OBSERVING and not self.restart_acknowledged:
            raise ValueError("status observation requires an acknowledged restart")
        if (self.observed_digest is None) != (self.observed_sequence is None):
            raise ValueError("observed release identity must be complete")
        if self.phase is RolloutPhase.ACTIVATING:
            if (
                self.receipt is not None
                or self.restart_attempts != 0
                or self.restart_acknowledged
                or self.status_polls != 0
                or self.observed_digest is not None
                or self.reason_code is not RolloutReason.ACTIVATION_REQUIRED
            ):
                raise ValueError("activating rollout state is inconsistent")
        elif self.phase is RolloutPhase.RESTARTING:
            if (
                self.receipt is None
                or self.restart_acknowledged
                or self.status_polls != 0
                or self.observed_digest is not None
                or self.reason_code
                not in {RolloutReason.RESTART_REQUIRED, RolloutReason.RESTART_IN_PROGRESS}
            ):
                raise ValueError("restarting rollout state is inconsistent")
        elif self.phase is RolloutPhase.OBSERVING:
            if (
                not self.restart_acknowledged
                or self.restart_attempts < 1
                or self.reason_code is not RolloutReason.AWAITING_TARGET_STATUS
            ):
                raise ValueError("observing rollout state is inconsistent")
        elif self.phase is RolloutPhase.VERIFIED:
            if (
                not self.restart_acknowledged
                or self.status_polls < 1
                or self.observed_digest != self.target.digest
                or self.observed_sequence != self.target.sequence
                or self.reason_code is not RolloutReason.TARGET_SERVING_VERIFIED
            ):
                raise ValueError("verified rollout state is inconsistent")
        elif self.reason_code not in {
            RolloutReason.ACTIVATION_ATTEMPTS_EXHAUSTED,
            RolloutReason.RESTART_ATTEMPTS_EXHAUSTED,
            RolloutReason.STATUS_POLLS_EXHAUSTED,
            RolloutReason.ROLLOUT_TIMEOUT,
            RolloutReason.RECONCILIATION_TIMEOUT,
            RolloutReason.TARGET_STATUS_REJECTED,
        }:
            raise ValueError("failed rollout reason is invalid")
        return self


class RolloutResult(StrictContract):
    rollout_id: UUID
    target: ReleaseIdentityResponse
    phase: RolloutPhase
    reason_code: ReasonCode
    activation_attempts: int = Field(ge=0, le=100)
    restart_attempts: int = Field(ge=0, le=100)
    status_polls: int = Field(ge=0, le=10000)
    observed_digest: Optional[Digest] = None
    observed_sequence: Optional[int] = Field(default=None, ge=0)
    complete: bool


class ActivationPort(Protocol):
    async def activate(
        self, candidate: ActivationCandidateRequest, *, operation_id: str
    ) -> Union[ActivationAcceptedResponse, Mapping[str, Any]]: ...


class WorkloadPort(Protocol):
    async def request_restart(
        self,
        *,
        operation_id: str,
        target_digest: str,
        target_sequence: int,
    ) -> None: ...


class StatusPort(Protocol):
    async def read_activation_status(
        self,
    ) -> Union[ActivationStatusResponse, Mapping[str, Any]]: ...


class RolloutStateStore(Protocol):
    def load(self) -> Optional[Mapping[str, Any]]: ...
    def save(self, value: Mapping[str, Any]) -> None: ...


class LocalRolloutStateStore:
    """Single-record JSON store with file and parent-directory durability."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise RolloutConfigurationError("state path must be an absolute Path")
        self._path = path

    def _validate_path(self) -> None:
        """Reject links anywhere in the existing state path ancestry."""
        current = Path(self._path.anchor)
        for part in self._path.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise RolloutStateError("rollout state path must not contain symlinks")
            if current.exists():
                if current == self._path:
                    if not current.is_file():
                        raise RolloutStateError("rollout state path is not a regular file")
                elif not current.is_dir():
                    raise RolloutStateError("rollout state parent is not a directory")

    def load(self) -> Optional[Mapping[str, Any]]:
        self._validate_path()
        if not self._path.exists():
            return None
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RolloutStateError("persisted rollout state is unreadable") from exc
        if not isinstance(value, dict):
            raise RolloutStateError("persisted rollout state is invalid")
        return value

    def save(self, value: Mapping[str, Any]) -> None:
        self._validate_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._validate_path()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", dir=str(self._path.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise RolloutStateError("persisted rollout state could not be written") from exc
        finally:
            if temporary.exists():
                temporary.unlink()


class DeploymentRolloutController:
    """Advance one rollout by at most one external operation per call."""

    def __init__(
        self,
        candidate: Union[ActivationCandidateRequest, Mapping[str, Any]],
        *,
        activation: ActivationPort,
        workload: WorkloadPort,
        status: StatusPort,
        store: RolloutStateStore,
        configuration: Optional[RolloutConfiguration] = None,
        clock: Optional[Callable[[], datetime]] = None,
        rollout_id_factory: Optional[Callable[[], UUID]] = None,
    ) -> None:
        try:
            self._candidate = (
                candidate
                if isinstance(candidate, ActivationCandidateRequest)
                else ActivationCandidateRequest.model_validate(candidate)
            )
            self._configuration = (
                configuration
                if isinstance(configuration, RolloutConfiguration)
                else RolloutConfiguration.model_validate(configuration or {})
            )
        except Exception as exc:
            raise RolloutConfigurationError("rollout configuration is invalid") from exc
        self._activation = activation
        self._workload = workload
        self._status = status
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rollout_id_factory = rollout_id_factory or uuid4

    def current_result(self) -> RolloutResult:
        """Return the durable rollout projection without calling an external port.

        The first read initializes the rollout record so every subsequent reader
        observes one stable rollout identifier.  It deliberately does not apply
        the elapsed-time policy: timeouts are state transitions and therefore
        happen only when an operator explicitly advances the rollout.
        """

        return self._result(self._load_or_initialize())

    async def advance(self) -> RolloutResult:
        state = self._load_or_initialize()
        if state.phase in {RolloutPhase.VERIFIED, RolloutPhase.FAILED}:
            return self._result(state)
        now = self._now(state.updated_at)
        elapsed = (now - state.started_at).total_seconds()
        if elapsed >= self._configuration.timeout_seconds:
            return self._persist_result(
                state.model_copy(
                    update={
                        "phase": RolloutPhase.FAILED,
                        "reason_code": RolloutReason.ROLLOUT_TIMEOUT,
                        "updated_at": now,
                    }
                )
            )

        if state.phase is RolloutPhase.ACTIVATING:
            attempt = state.activation_attempts + 1
            state = self._persist(
                state.model_copy(
                    update={"activation_attempts": attempt, "updated_at": now}
                )
            )
            operation_id = f"{state.rollout_id}:activate"
            try:
                raw = await self._activation.activate(
                    self._candidate, operation_id=operation_id
                )
                accepted = (
                    raw
                    if isinstance(raw, ActivationAcceptedResponse)
                    else ActivationAcceptedResponse.model_validate(raw)
                )
            except Exception as exc:
                if attempt >= self._configuration.maximum_activation_attempts:
                    return self._persist_result(
                        state.model_copy(
                            update={
                                "phase": RolloutPhase.FAILED,
                                "reason_code": RolloutReason.ACTIVATION_ATTEMPTS_EXHAUSTED,
                                "updated_at": now,
                            }
                        )
                    )
                raise RolloutPortError("activation operation failed") from exc
            if accepted.receipt.desired != state.target:
                raise RolloutPortError("activation response target is invalid")
            return self._persist_result(
                state.model_copy(
                    update={
                        "phase": RolloutPhase.RESTARTING,
                        "reason_code": RolloutReason.RESTART_REQUIRED,
                        "receipt": accepted,
                        "updated_at": now,
                    }
                )
            )

        if state.phase is RolloutPhase.RESTARTING:
            attempt = state.restart_attempts + 1
            state = self._persist(
                state.model_copy(
                    update={
                        "restart_attempts": attempt,
                        "reason_code": RolloutReason.RESTART_IN_PROGRESS,
                        "updated_at": now,
                    }
                )
            )
            # Retries use one stable operation ID.  A transport failure may
            # occur after the remote side accepted the request, so changing
            # the key could accidentally initiate a second restart.
            operation_id = f"{state.rollout_id}:restart:1"
            try:
                await self._workload.request_restart(
                    operation_id=operation_id,
                    target_digest=state.target.digest,
                    target_sequence=state.target.sequence,
                )
            except Exception as exc:
                if attempt >= self._configuration.maximum_restart_attempts:
                    return self._persist_result(
                        state.model_copy(
                            update={
                                "phase": RolloutPhase.FAILED,
                                "reason_code": RolloutReason.RESTART_ATTEMPTS_EXHAUSTED,
                                "updated_at": now,
                            }
                        )
                    )
                raise RolloutPortError("workload restart operation failed") from exc
            return self._persist_result(
                state.model_copy(
                    update={
                        "phase": RolloutPhase.OBSERVING,
                        "reason_code": RolloutReason.AWAITING_TARGET_STATUS,
                        "restart_acknowledged": True,
                        "updated_at": now,
                    }
                )
            )

        # OBSERVING is the only remaining non-terminal phase.
        try:
            raw_status = await self._status.read_activation_status()
            observed = (
                raw_status
                if isinstance(raw_status, ActivationStatusResponse)
                else ActivationStatusResponse.model_validate(raw_status)
            )
        except Exception as exc:
            raise RolloutPortError("activation status operation failed") from exc

        polls = state.status_polls + 1
        reconciled = DeploymentReconciler(
            state.receipt.receipt,
            maximum_attempts=self._configuration.maximum_restart_attempts,
            timeout_seconds=self._configuration.timeout_seconds,
        ).evaluate(
            observed,
            restart_attempts=state.restart_attempts,
            elapsed_seconds=elapsed,
        )
        phase = state.phase
        reason = RolloutReason.AWAITING_TARGET_STATUS
        if reconciled.state is ReconciliationState.VERIFIED:
            phase = RolloutPhase.VERIFIED
            reason = RolloutReason.TARGET_SERVING_VERIFIED
        elif reconciled.state is ReconciliationState.FAILED:
            phase = RolloutPhase.FAILED
            reason = RolloutReason(reconciled.reason_code.value)
        elif polls >= self._configuration.maximum_status_polls:
            phase = RolloutPhase.FAILED
            reason = RolloutReason.STATUS_POLLS_EXHAUSTED

        return self._persist_result(
            state.model_copy(
                update={
                    "phase": phase,
                    "reason_code": reason,
                    "status_polls": polls,
                    "observed_digest": observed.active_digest,
                    "observed_sequence": observed.active_sequence
                    if observed.active_digest is not None
                    else None,
                    "updated_at": now,
                }
            )
        )

    def _load_or_initialize(self) -> PersistedRolloutState:
        try:
            raw = self._store.load()
        except RolloutStateError:
            raise
        except Exception as exc:
            raise RolloutStateError("persisted rollout state could not be loaded") from exc
        target = ReleaseIdentityResponse(
            digest=self._candidate.digest, sequence=self._candidate.sequence
        )
        if raw is None:
            now = self._now()
            state = PersistedRolloutState(
                rollout_id=self._rollout_id_factory(),
                target=target,
                phase=RolloutPhase.ACTIVATING,
                reason_code=RolloutReason.ACTIVATION_REQUIRED,
                started_at=now,
                updated_at=now,
            )
            return self._persist(state)
        try:
            state = PersistedRolloutState.model_validate(raw)
        except Exception as exc:
            raise RolloutStateError("persisted rollout state is invalid") from exc
        if state.target != target:
            raise RolloutStateError("persisted rollout target does not match candidate")
        return state

    def _persist(self, state: PersistedRolloutState) -> PersistedRolloutState:
        try:
            validated = PersistedRolloutState.model_validate(state)
            # Nested activation contracts use wire aliases (notably
            # transition ``from``/``to``); persist their canonical form so a
            # new controller process can validate the record again.
            self._store.save(validated.model_dump(mode="json", by_alias=True))
        except RolloutStateError:
            raise
        except Exception as exc:
            raise RolloutStateError("persisted rollout state could not be written") from exc
        return validated

    def _persist_result(self, state: PersistedRolloutState) -> RolloutResult:
        return self._result(self._persist(state))

    def _now(self, floor: Optional[datetime] = None) -> datetime:
        try:
            value = self._clock()
        except Exception as exc:
            raise RolloutStateError("rollout clock failed") from exc
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RolloutStateError("rollout clock must return a timezone-aware datetime")
        if floor is not None and value < floor:
            raise RolloutStateError("rollout clock moved backward")
        return value

    @staticmethod
    def _result(state: PersistedRolloutState) -> RolloutResult:
        return RolloutResult(
            rollout_id=state.rollout_id,
            target=state.target,
            phase=state.phase,
            reason_code=state.reason_code,
            activation_attempts=state.activation_attempts,
            restart_attempts=state.restart_attempts,
            status_polls=state.status_polls,
            observed_digest=state.observed_digest,
            observed_sequence=state.observed_sequence,
            complete=state.phase in {RolloutPhase.VERIFIED, RolloutPhase.FAILED},
        )
