"""Tests for bounded activation-to-serving deployment reconciliation."""
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from backend.services.rag_service.deployment_reconciliation import (
    DeploymentReconciler,
    ReconciliationError,
    ReconciliationReason,
    ReconciliationState,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _receipt(**changes):
    value = {
        "receipt_id": "89a8bb70-bf06-4a1e-89a6-306516e93d8f",
        "desired": {"digest": DIGEST_A, "sequence": 7},
        "activated": {"digest": DIGEST_A, "sequence": 7},
        "previous_digest": DIGEST_B,
        "transition": {"from": "READY", "to": "ACTIVE"},
        "result": "success",
        "reason_code": "ACTIVATED",
        "device_counter": 12,
        "created_at": datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc),
    }
    value.update(changes)
    return value


def _status(**changes):
    value = {
        "active_digest": DIGEST_A,
        "active_sequence": 7,
        "sequence_floor": 7,
        "mode": "production",
        "state": "ACTIVE",
        "ready": True,
        "reason_code": "READY",
    }
    value.update(changes)
    return value


def test_http_acceptance_alone_is_pending_not_serving_completion() -> None:
    reconciler = DeploymentReconciler(_receipt())

    result = reconciler.evaluate(None, restart_attempts=0, elapsed_seconds=0)

    assert result.state is ReconciliationState.PENDING
    assert result.reason_code is ReconciliationReason.RESTART_REQUIRED
    assert result.complete is False


def test_matching_status_before_restart_does_not_verify() -> None:
    result = DeploymentReconciler(_receipt()).evaluate(
        _status(), restart_attempts=0, elapsed_seconds=1
    )

    assert result.state is ReconciliationState.PENDING
    assert result.reason_code is ReconciliationReason.RESTART_REQUIRED


def test_distinguishes_restart_in_progress_and_old_runtime_status() -> None:
    reconciler = DeploymentReconciler(_receipt())

    in_progress = reconciler.evaluate(
        None,
        restart_attempts=1,
        elapsed_seconds=10,
        restart_in_progress=True,
    )
    old_runtime = reconciler.evaluate(
        _status(active_digest=DIGEST_B, active_sequence=6, sequence_floor=6),
        restart_attempts=1,
        elapsed_seconds=20,
    )

    assert in_progress.state is ReconciliationState.RESTARTING
    assert in_progress.reason_code is ReconciliationReason.RESTART_IN_PROGRESS
    assert old_runtime.state is ReconciliationState.RESTARTING
    assert old_runtime.reason_code is ReconciliationReason.AWAITING_TARGET_STATUS


def test_verifies_only_exact_post_restart_production_active_ready_status() -> None:
    reconciler = DeploymentReconciler(_receipt())

    result = reconciler.evaluate(
        _status(), restart_attempts=1, elapsed_seconds=24
    )

    assert result.state is ReconciliationState.VERIFIED
    assert result.reason_code is ReconciliationReason.TARGET_SERVING_VERIFIED
    assert result.observed_digest == DIGEST_A
    assert result.observed_sequence == 7
    assert result.complete is True


@pytest.mark.parametrize(
    "status",
    [
        _status(
            mode="recovery",
            state="RECOVERY",
            reason_code="RECOVERY_ACTIVE",
            sequence_floor=9,
        ),
        _status(
            state="REJECTED",
            ready=False,
            reason_code="ACTIVATION_FAILED",
        ),
    ],
)
def test_matching_target_in_non_serving_or_recovery_state_fails(status) -> None:
    result = DeploymentReconciler(_receipt()).evaluate(
        status, restart_attempts=1, elapsed_seconds=10
    )

    assert result.state is ReconciliationState.FAILED
    assert result.reason_code is ReconciliationReason.TARGET_STATUS_REJECTED


def test_wrong_digest_or_sequence_never_verifies() -> None:
    reconciler = DeploymentReconciler(_receipt(), maximum_attempts=3)

    result = reconciler.evaluate(
        _status(active_digest=DIGEST_B), restart_attempts=1, elapsed_seconds=5
    )

    assert result.state is ReconciliationState.RESTARTING
    assert result.observed_digest == DIGEST_B


def test_attempt_budget_fails_closed() -> None:
    result = DeploymentReconciler(_receipt(), maximum_attempts=2).evaluate(
        None, restart_attempts=2, elapsed_seconds=30
    )

    assert result.state is ReconciliationState.FAILED
    assert result.reason_code is ReconciliationReason.RESTART_ATTEMPTS_EXHAUSTED
    assert result.restart_attempts == 2


def test_time_budget_fails_closed_and_bounds_reported_elapsed() -> None:
    result = DeploymentReconciler(_receipt(), timeout_seconds=60).evaluate(
        None, restart_attempts=1, elapsed_seconds=600
    )

    assert result.state is ReconciliationState.FAILED
    assert result.reason_code is ReconciliationReason.RECONCILIATION_TIMEOUT
    assert result.elapsed_seconds == 60


def test_exact_status_first_observed_after_deadline_does_not_revive() -> None:
    result = DeploymentReconciler(_receipt(), timeout_seconds=60).evaluate(
        _status(), restart_attempts=1, elapsed_seconds=61
    )

    assert result.state is ReconciliationState.FAILED
    assert result.reason_code is ReconciliationReason.RECONCILIATION_TIMEOUT


def test_terminal_result_cannot_regress_or_late_verify() -> None:
    reconciler = DeploymentReconciler(_receipt(), maximum_attempts=1)
    failed = reconciler.evaluate(None, restart_attempts=1, elapsed_seconds=10)

    later = reconciler.evaluate(_status(), restart_attempts=2, elapsed_seconds=20)

    assert later is failed
    assert later.state is ReconciliationState.FAILED


def test_result_is_fixed_size_and_contains_no_paths_or_secrets() -> None:
    result = DeploymentReconciler(_receipt()).evaluate(
        None, restart_attempts=0, elapsed_seconds=0
    )
    value = asdict(result)

    assert set(value) == {
        "state",
        "reason_code",
        "target_digest",
        "target_sequence",
        "observed_digest",
        "observed_sequence",
        "restart_attempts",
        "maximum_attempts",
        "elapsed_seconds",
        "timeout_seconds",
    }
    serialized = str(value).lower()
    for forbidden in ("path", "secret", "token", "credential", "message", "content"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "receipt_change",
    [
        {"result": "rejected", "activated": None,
         "transition": {"from": "READY", "to": "REJECTED"}},
        {"activated": {"digest": DIGEST_B, "sequence": 7}},
    ],
)
def test_requires_coherent_accepted_production_receipt(receipt_change) -> None:
    with pytest.raises(ReconciliationError, match="receipt|accepted production"):
        DeploymentReconciler(_receipt(**receipt_change))


@pytest.mark.parametrize(
    "arguments",
    [
        {"restart_attempts": -1, "elapsed_seconds": 0},
        {"restart_attempts": 0, "elapsed_seconds": -1},
        {"restart_attempts": 0, "elapsed_seconds": float("inf")},
        {
            "restart_attempts": 0,
            "elapsed_seconds": 0,
            "restart_in_progress": True,
        },
    ],
)
def test_rejects_invalid_progress_counters(arguments) -> None:
    with pytest.raises(ReconciliationError):
        DeploymentReconciler(_receipt()).evaluate(None, **arguments)


def test_rejects_malformed_observed_status() -> None:
    with pytest.raises(ReconciliationError, match="observed"):
        DeploymentReconciler(_receipt()).evaluate(
            {"ready": True, "package_path": "/secret"},
            restart_attempts=1,
            elapsed_seconds=1,
        )


def test_state_machine_rejects_attempt_or_time_regression() -> None:
    reconciler = DeploymentReconciler(_receipt())
    reconciler.evaluate(None, restart_attempts=1, elapsed_seconds=20)

    with pytest.raises(ReconciliationError, match="backward"):
        reconciler.evaluate(None, restart_attempts=0, elapsed_seconds=21)
    with pytest.raises(ReconciliationError, match="backward"):
        reconciler.evaluate(None, restart_attempts=1, elapsed_seconds=19)
