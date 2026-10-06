from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.services.rag_service.activation_contracts import (
    ActivationAcceptedResponse,
    ActivationCandidateRequest,
    ActivationMode,
    ActivationReceiptResponse,
    ActivationResult,
    ActivationStatusResponse,
    ActivationStatusState,
    ActivationTransitionResponse,
    ReleaseIdentityResponse,
)
from backend.services.rag_service.deployment_rollout import (
    DeploymentRolloutController,
    LocalRolloutStateStore,
    RolloutConfiguration,
    RolloutConfigurationError,
    RolloutPhase,
    RolloutPortError,
    RolloutReason,
    RolloutStateError,
)


DIGEST = "sha256:" + "a" * 64
OLD_DIGEST = "sha256:" + "b" * 64
START = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
ROLLOUT_ID = UUID("11111111-1111-4111-8111-111111111111")


def _candidate(path: str = "/intake/release") -> ActivationCandidateRequest:
    return ActivationCandidateRequest(digest=DIGEST, sequence=7, package_path=path)


def _accepted(digest: str = DIGEST, sequence: int = 7) -> ActivationAcceptedResponse:
    desired = ReleaseIdentityResponse(digest=digest, sequence=sequence)
    return ActivationAcceptedResponse(
        receipt=ActivationReceiptResponse(
            receipt_id=UUID("22222222-2222-4222-8222-222222222222"),
            desired=desired,
            activated=desired,
            previous_digest=OLD_DIGEST,
            transition=ActivationTransitionResponse.model_validate(
                {"from": "ACTIVE", "to": "ACTIVE"}
            ),
            result=ActivationResult.SUCCESS,
            reason_code="ACTIVATED",
            device_counter=8,
            created_at=START,
        )
    )


def _status(
    digest: str = DIGEST,
    sequence: int = 7,
    *,
    ready: bool = True,
) -> ActivationStatusResponse:
    return ActivationStatusResponse(
        active_digest=digest,
        active_sequence=sequence,
        sequence_floor=max(sequence, 7),
        mode=ActivationMode.PRODUCTION,
        state=ActivationStatusState.ACTIVE if ready else ActivationStatusState.REJECTED,
        ready=ready,
        reason_code="READY" if ready else "CORPUS_REJECTED",
    )


class MemoryStore:
    def __init__(self) -> None:
        self.value = None
        self.saves: list[dict] = []

    def load(self):
        return self.value

    def save(self, value):
        self.value = json.loads(json.dumps(value))
        self.saves.append(self.value)


class Activation:
    def __init__(self, response=None) -> None:
        self.response = response or _accepted()
        self.calls = []
        self.error = None

    async def activate(self, candidate, *, operation_id):
        self.calls.append((candidate, operation_id))
        if self.error:
            raise self.error
        return self.response


class Workload:
    def __init__(self) -> None:
        self.calls = []
        self.error = None

    async def request_restart(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


class Status:
    def __init__(self, value=None) -> None:
        self.value = value or _status()
        self.calls = 0
        self.error = None

    async def read_activation_status(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.value


class Clock:
    def __init__(self, now=START) -> None:
        self.now = now

    def __call__(self):
        return self.now


def _controller(store=None, activation=None, workload=None, status=None, clock=None, **config):
    return DeploymentRolloutController(
        _candidate(),
        activation=activation or Activation(),
        workload=workload or Workload(),
        status=status or Status(),
        store=store or MemoryStore(),
        configuration=RolloutConfiguration(**config),
        clock=clock or Clock(),
        rollout_id_factory=lambda: ROLLOUT_ID,
    )


@pytest.mark.asyncio
async def test_happy_path_requires_activation_restart_and_post_restart_status():
    store, activation, workload, status = MemoryStore(), Activation(), Workload(), Status()
    controller = _controller(
        store=store, activation=activation, workload=workload, status=status
    )

    activated = await controller.advance()
    assert activated.phase is RolloutPhase.RESTARTING
    assert activated.reason_code == RolloutReason.RESTART_REQUIRED.value
    assert status.calls == 0

    restarted = await controller.advance()
    assert restarted.phase is RolloutPhase.OBSERVING
    assert restarted.restart_attempts == 1
    assert workload.calls == [
        {
            "operation_id": f"{ROLLOUT_ID}:restart:1",
            "target_digest": DIGEST,
            "target_sequence": 7,
        }
    ]

    verified = await controller.advance()
    assert verified.phase is RolloutPhase.VERIFIED
    assert verified.reason_code == RolloutReason.TARGET_SERVING_VERIFIED.value
    assert verified.complete is True
    assert verified.observed_digest == DIGEST
    assert await controller.advance() == verified
    assert status.calls == 1


@pytest.mark.asyncio
async def test_activation_intent_is_persisted_before_call_and_reuses_operation_id():
    store, activation = MemoryStore(), Activation()
    activation.error = RuntimeError("sensitive endpoint")
    controller = _controller(store=store, activation=activation)

    with pytest.raises(RolloutPortError, match="activation operation failed") as failure:
        await controller.advance()
    assert "sensitive" not in str(failure.value)
    assert store.value["phase"] == "activating"
    first_id = activation.calls[0][1]

    activation.error = None
    resumed = _controller(store=store, activation=activation)
    assert (await resumed.advance()).phase is RolloutPhase.RESTARTING
    assert activation.calls[1][1] == first_id


@pytest.mark.asyncio
async def test_activation_transport_retry_budget_is_durable_and_terminal():
    store, activation = MemoryStore(), Activation()
    activation.error = RuntimeError("unavailable")
    controller = _controller(
        store=store, activation=activation, maximum_activation_attempts=2
    )
    with pytest.raises(RolloutPortError):
        await controller.advance()
    result = await _controller(
        store=store, activation=activation, maximum_activation_attempts=2
    ).advance()
    assert result.phase is RolloutPhase.FAILED
    assert result.reason_code == RolloutReason.ACTIVATION_ATTEMPTS_EXHAUSTED.value
    assert result.activation_attempts == 2
    assert len({call[1] for call in activation.calls}) == 1


@pytest.mark.asyncio
async def test_restart_intent_is_persisted_before_call_and_replays_same_attempt():
    store, workload = MemoryStore(), Workload()
    controller = _controller(store=store, workload=workload)
    await controller.advance()
    workload.error = RuntimeError("secret transport detail")

    with pytest.raises(RolloutPortError, match="workload restart operation failed"):
        await controller.advance()
    assert store.value["restart_attempts"] == 1
    assert store.value["restart_acknowledged"] is False
    first = workload.calls[0]["operation_id"]

    workload.error = None
    resumed = _controller(store=store, workload=workload)
    assert (await resumed.advance()).phase is RolloutPhase.OBSERVING
    assert workload.calls[1]["operation_id"] == first


@pytest.mark.asyncio
async def test_restart_transport_retry_budget_is_durable_and_terminal():
    store, workload = MemoryStore(), Workload()
    workload.error = RuntimeError("unavailable")
    controller = _controller(
        store=store, workload=workload, maximum_restart_attempts=2
    )
    await controller.advance()
    with pytest.raises(RolloutPortError):
        await controller.advance()
    result = await _controller(
        store=store, workload=workload, maximum_restart_attempts=2
    ).advance()
    assert result.phase is RolloutPhase.FAILED
    assert result.reason_code == RolloutReason.RESTART_ATTEMPTS_EXHAUSTED.value
    assert result.restart_attempts == 2
    assert len({call["operation_id"] for call in workload.calls}) == 1


@pytest.mark.asyncio
async def test_old_status_never_completes_and_poll_budget_fails_closed():
    store = MemoryStore()
    status = Status(_status(OLD_DIGEST, 6))
    controller = _controller(store=store, status=status, maximum_status_polls=2)
    await controller.advance()
    await controller.advance()

    pending = await controller.advance()
    assert pending.phase is RolloutPhase.OBSERVING
    failed = await controller.advance()
    assert failed.phase is RolloutPhase.FAILED
    assert failed.reason_code == RolloutReason.STATUS_POLLS_EXHAUSTED.value
    assert failed.complete is True
    assert status.calls == 2


@pytest.mark.asyncio
async def test_target_status_that_is_not_ready_is_rejected():
    controller = _controller(status=Status(_status(ready=False)))
    await controller.advance()
    await controller.advance()
    result = await controller.advance()
    assert result.phase is RolloutPhase.FAILED
    assert result.reason_code == RolloutReason.TARGET_STATUS_REJECTED.value


@pytest.mark.asyncio
async def test_timeout_survives_controller_reconstruction():
    store, clock = MemoryStore(), Clock()
    first = _controller(store=store, clock=clock, timeout_seconds=10)
    await first.advance()
    await first.advance()
    clock.now += timedelta(seconds=11)

    resumed = _controller(store=store, clock=clock, timeout_seconds=10)
    result = await resumed.advance()
    assert result.phase is RolloutPhase.FAILED
    assert result.reason_code == RolloutReason.ROLLOUT_TIMEOUT.value


@pytest.mark.asyncio
async def test_deadline_prevents_activation_retry_after_reconstruction():
    store, clock, activation = MemoryStore(), Clock(), Activation()
    activation.error = RuntimeError("offline")
    controller = _controller(
        store=store, clock=clock, activation=activation, timeout_seconds=10
    )
    with pytest.raises(RolloutPortError):
        await controller.advance()
    clock.now += timedelta(seconds=10)

    result = await _controller(
        store=store, clock=clock, activation=activation, timeout_seconds=10
    ).advance()
    assert result.reason_code == RolloutReason.ROLLOUT_TIMEOUT.value
    assert len(activation.calls) == 1


@pytest.mark.asyncio
async def test_deadline_prevents_restart_request_after_reconstruction():
    store, clock, workload = MemoryStore(), Clock(), Workload()
    controller = _controller(
        store=store, clock=clock, workload=workload, timeout_seconds=10
    )
    await controller.advance()
    workload.error = RuntimeError("offline")
    with pytest.raises(RolloutPortError):
        await controller.advance()
    clock.now += timedelta(seconds=10)

    result = await _controller(
        store=store, clock=clock, workload=workload, timeout_seconds=10
    ).advance()
    assert result.reason_code == RolloutReason.ROLLOUT_TIMEOUT.value
    assert len(workload.calls) == 1


@pytest.mark.asyncio
async def test_deadline_prevents_status_retry_after_failure():
    store, clock, status = MemoryStore(), Clock(), Status()
    controller = _controller(
        store=store, clock=clock, status=status, timeout_seconds=10
    )
    await controller.advance()
    await controller.advance()
    status.error = RuntimeError("offline")
    with pytest.raises(RolloutPortError):
        await controller.advance()
    clock.now += timedelta(seconds=10)

    result = await _controller(
        store=store, clock=clock, status=status, timeout_seconds=10
    ).advance()
    assert result.reason_code == RolloutReason.ROLLOUT_TIMEOUT.value
    assert status.calls == 1


@pytest.mark.asyncio
async def test_status_failure_is_bounded_and_does_not_consume_poll():
    store, status = MemoryStore(), Status()
    controller = _controller(store=store, status=status)
    await controller.advance()
    await controller.advance()
    status.error = RuntimeError("credential=do-not-print")

    with pytest.raises(RolloutPortError) as failure:
        await controller.advance()
    assert str(failure.value) == "activation status operation failed"
    assert store.value["status_polls"] == 0


@pytest.mark.asyncio
async def test_activation_response_must_match_target():
    activation = Activation(_accepted("sha256:" + "c" * 64, 8))
    with pytest.raises(RolloutPortError, match="target is invalid"):
        await _controller(activation=activation).advance()


@pytest.mark.asyncio
async def test_persisted_state_rejects_changed_candidate_and_unknown_fields():
    store = MemoryStore()
    controller = _controller(store=store)
    await controller.advance()

    changed = DeploymentRolloutController(
        ActivationCandidateRequest(
            digest="sha256:" + "c" * 64,
            sequence=8,
            package_path="/other/location",
        ),
        activation=Activation(), workload=Workload(), status=Status(), store=store,
    )
    with pytest.raises(RolloutStateError, match="does not match"):
        await changed.advance()

    store.value["unexpected"] = True
    with pytest.raises(RolloutStateError, match="invalid"):
        await controller.advance()


@pytest.mark.asyncio
async def test_corrupt_terminal_state_cannot_falsely_report_verified():
    store = MemoryStore()
    controller = _controller(store=store)
    await controller.advance()
    await controller.advance()
    assert (await controller.advance()).phase is RolloutPhase.VERIFIED
    store.value["observed_digest"] = OLD_DIGEST

    with pytest.raises(RolloutStateError, match="invalid"):
        await _controller(store=store).advance()


@pytest.mark.parametrize(
    "value",
    [
        {"maximum_restart_attempts": 0},
        {"maximum_activation_attempts": 0},
        {"maximum_status_polls": 0},
        {"timeout_seconds": 0},
        {"maximum_status_polls": 10001},
    ],
)
def test_configuration_is_strict_and_bounded(value):
    with pytest.raises(Exception):
        RolloutConfiguration(**value)


def test_invalid_candidate_is_reported_as_configuration_error():
    with pytest.raises(RolloutConfigurationError):
        DeploymentRolloutController(
            {"digest": "bad", "sequence": 1, "package_path": "/intake/a"},
            activation=Activation(), workload=Workload(), status=Status(), store=MemoryStore(),
        )


@pytest.mark.asyncio
async def test_clock_must_be_aware_and_monotonic():
    store = MemoryStore()
    with pytest.raises(RolloutStateError, match="timezone-aware"):
        await _controller(store=store, clock=Clock(datetime(2026, 1, 1))).advance()

    controller = _controller(store=store)
    await controller.advance()
    with pytest.raises(RolloutStateError, match="moved backward"):
        await _controller(store=store, clock=Clock(START - timedelta(seconds=1))).advance()


@pytest.mark.asyncio
async def test_persisted_projection_contains_no_candidate_path_or_port_exception():
    store = MemoryStore()
    await _controller(store=store).advance()
    serialized = json.dumps(store.value)
    assert "/intake/release" not in serialized
    assert "package_path" not in serialized


def test_local_store_round_trip_and_corruption(tmp_path: Path):
    path = tmp_path / "state" / "rollout.json"
    store = LocalRolloutStateStore(path.resolve())
    assert store.load() is None
    store.save({"safe": "value"})
    assert store.load() == {"safe": "value"}

    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(RolloutStateError, match="unreadable"):
        store.load()


def test_local_store_rejects_final_path_symlink(tmp_path: Path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    linked = tmp_path / "rollout.json"
    linked.symlink_to(target)
    store = LocalRolloutStateStore(linked.resolve(strict=False).parent / linked.name)
    with pytest.raises(RolloutStateError, match="symlinks"):
        store.load()
    with pytest.raises(RolloutStateError, match="symlinks"):
        store.save({"safe": True})


def test_local_store_rejects_symlinked_parent(tmp_path: Path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    store = LocalRolloutStateStore(tmp_path.resolve() / "linked" / "rollout.json")
    with pytest.raises(RolloutStateError, match="symlinks"):
        store.load()
    with pytest.raises(RolloutStateError, match="symlinks"):
        store.save({"safe": True})


def test_local_store_keeps_state_as_regular_non_symlink_file(tmp_path: Path):
    path = tmp_path.resolve() / "state" / "rollout.json"
    store = LocalRolloutStateStore(path)
    store.save({"safe": True})
    assert path.is_file()
    assert not path.is_symlink()


def test_local_store_requires_absolute_path():
    with pytest.raises(RolloutConfigurationError, match="absolute"):
        LocalRolloutStateStore(Path("relative.json"))
