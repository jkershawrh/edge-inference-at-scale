"""Desired-release fleet compliance stays tied to live serving evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from backend.services.node_manager.fleet_compliance import (
    MAX_FLEET_NODES,
    DesiredReleaseCompliancePolicy,
    FleetComplianceError,
)
from backend.services.node_manager.main import NodeManager


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _status(character: str, sequence: int, **overrides):
    value = {
        "active_digest": _digest(character),
        "active_sequence": sequence,
        "sequence_floor": sequence,
        "mode": "production",
        "state": "ACTIVE",
        "ready": True,
        "reason_code": "READY",
    }
    value.update(overrides)
    return value


def _acceptance(character: str, sequence: int):
    identity = {"digest": _digest(character), "sequence": sequence}
    return {
        "receipt": {
            "receipt_id": str(UUID(int=sequence)),
            "desired": identity,
            "activated": identity,
            "previous_digest": _digest("0"),
            "transition": {"from": "ACTIVE", "to": "ACTIVE"},
            "result": "success",
            "reason_code": "ACTIVATED",
            "device_counter": sequence,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        },
        "live_reload_performed": False,
        "restart_or_reconciliation_required": True,
    }


def _observation(node_id: str, **overrides):
    value = {
        "node_id": node_id,
        "online": True,
        "activation": _status("a", 7),
        "accepted_activation": None,
    }
    value.update(overrides)
    return value


def _policy() -> DesiredReleaseCompliancePolicy:
    return DesiredReleaseCompliancePolicy({"digest": _digest("b"), "sequence": 8})


def test_live_exact_ready_production_is_compliant_without_receipt() -> None:
    report = _policy().evaluate(
        [_observation("node-a", activation=_status("b", 8))]
    )

    assert report.fleet_compliant is True
    assert report.counts.compliant == 1
    assert report.nodes[0].model_dump(mode="json") == {
        "node_id": "node-a",
        "classification": "compliant",
        "observed_digest": _digest("b"),
        "observed_sequence": 8,
    }


def test_exact_receipt_with_old_live_release_is_only_pending_restart() -> None:
    report = _policy().evaluate(
        [_observation("node-a", accepted_activation=_acceptance("b", 8))]
    )

    assert report.fleet_compliant is False
    assert report.counts.pending_restart == 1
    assert report.nodes[0].classification == "pending_restart"


@pytest.mark.parametrize("online,activation", [(False, _status("b", 8)), (True, None)])
def test_receipt_alone_never_makes_unknown_node_compliant(online, activation) -> None:
    report = _policy().evaluate(
        [
            _observation(
                "node-a",
                online=online,
                activation=activation,
                accepted_activation=_acceptance("b", 8),
            )
        ]
    )

    assert report.nodes[0].classification == "unknown"
    assert report.fleet_compliant is False


def test_recovery_takes_precedence_even_when_digest_and_sequence_match() -> None:
    recovery = _status(
        "b",
        8,
        sequence_floor=9,
        mode="recovery",
        state="RECOVERY",
        reason_code="RECOVERY_ACTIVE",
    )
    report = _policy().evaluate(
        [
            _observation(
                "node-a",
                activation=recovery,
                accepted_activation=_acceptance("b", 8),
            )
        ]
    )

    assert report.nodes[0].classification == "recovery"
    assert report.counts.recovery == 1


@pytest.mark.parametrize(
    "activation",
    [
        {
            "active_digest": None,
            "active_sequence": 0,
            "sequence_floor": 0,
            "mode": "uninitialized",
            "state": "UNINITIALIZED",
            "ready": False,
            "reason_code": "NO_ACTIVE_CORPUS",
        },
        _status("b", 8, ready=False, state="REJECTED", reason_code="LOAD_FAILED"),
    ],
)
def test_unready_takes_precedence_over_receipt(activation) -> None:
    report = _policy().evaluate(
        [
            _observation(
                "node-a",
                activation=activation,
                accepted_activation=_acceptance("b", 8),
            )
        ]
    )

    assert report.nodes[0].classification == "unready"


@pytest.mark.parametrize(
    "activation",
    [_status("a", 8), _status("b", 7)],
)
def test_digest_or_sequence_mismatch_is_drift_without_exact_acceptance(activation) -> None:
    report = _policy().evaluate([_observation("node-a", activation=activation)])

    assert report.nodes[0].classification == "drift"
    assert report.counts.drift == 1


def test_acceptance_for_a_different_release_does_not_hide_drift() -> None:
    report = _policy().evaluate(
        [_observation("node-a", accepted_activation=_acceptance("c", 9))]
    )

    assert report.nodes[0].classification == "drift"


def test_mixed_report_has_deterministic_order_and_exact_counts() -> None:
    observations = [
        _observation("z-unknown", online=False),
        _observation("d-drift"),
        _observation("a-compliant", activation=_status("b", 8)),
        _observation("c-pending", accepted_activation=_acceptance("b", 8)),
        _observation(
            "b-recovery",
            activation=_status(
                "a",
                6,
                sequence_floor=8,
                mode="recovery",
                state="RECOVERY",
                reason_code="RECOVERY_ACTIVE",
            ),
        ),
        _observation(
            "e-unready",
            activation={
                "active_digest": None,
                "active_sequence": 0,
                "sequence_floor": 0,
                "mode": "uninitialized",
                "state": "UNINITIALIZED",
                "ready": False,
                "reason_code": "NO_ACTIVE_CORPUS",
            },
        ),
    ]

    first = _policy().evaluate(observations).model_dump(mode="json")
    second = _policy().evaluate(reversed(observations)).model_dump(mode="json")

    assert first == second
    assert [node["node_id"] for node in first["nodes"]] == sorted(
        node["node_id"] for node in first["nodes"]
    )
    assert first["counts"] == {
        "compliant": 1,
        "pending_restart": 1,
        "recovery": 1,
        "drift": 1,
        "unready": 1,
        "unknown": 1,
    }
    assert first["fleet_compliant"] is False


def test_empty_fleet_is_not_compliant() -> None:
    report = _policy().evaluate([])

    assert report.total_nodes == 0
    assert report.fleet_compliant is False
    assert report.nodes == []


@pytest.mark.parametrize(
    "desired",
    [
        {"digest": "bad", "sequence": 1},
        {"digest": _digest("a"), "sequence": 0},
        {"digest": _digest("a"), "sequence": 1, "path": "/secret"},
    ],
)
def test_invalid_desired_release_fails_closed(desired) -> None:
    with pytest.raises(FleetComplianceError, match="desired release is invalid"):
        DesiredReleaseCompliancePolicy(desired)


def test_invalid_or_duplicate_observation_fails_closed() -> None:
    with pytest.raises(FleetComplianceError, match="fleet observation is invalid"):
        _policy().evaluate([{"node_id": "../node", "online": True}])

    with pytest.raises(FleetComplianceError, match="duplicate node"):
        _policy().evaluate([_observation("node-a"), _observation("node-a")])


def test_non_node_iterable_and_size_limit_are_rejected() -> None:
    with pytest.raises(FleetComplianceError, match="iterable of nodes"):
        _policy().evaluate({"node-a": _observation("node-a")})

    observations = (
        _observation(f"node-{index}") for index in range(MAX_FLEET_NODES + 1)
    )
    with pytest.raises(FleetComplianceError, match="cannot exceed"):
        _policy().evaluate(observations)


def test_output_has_only_bounded_release_identity_and_classification() -> None:
    report = _policy().evaluate(
        [_observation("node-a", accepted_activation=_acceptance("b", 8))]
    )
    payload = report.model_dump(mode="json")
    serialized = str(payload).lower()

    assert set(payload) == {
        "desired",
        "fleet_compliant",
        "total_nodes",
        "counts",
        "nodes",
    }
    assert "receipt" not in serialized
    assert "token" not in serialized
    assert "path" not in serialized
    assert "/" not in serialized


def test_node_manager_builds_report_from_live_registered_state() -> None:
    manager = NodeManager()
    manager.register_node("node-a", "http://node-a:8000", {})
    manager.register_node("node-b", "http://node-b:8000", {})
    manager.heartbeat("node-a", {}, _status("b", 8))
    manager.heartbeat("node-b", {}, _status("a", 7))

    report = manager.get_desired_release_compliance(
        {"digest": _digest("b"), "sequence": 8},
        accepted_activations={"node-b": _acceptance("b", 8)},
    )

    assert report.counts.compliant == 1
    assert report.counts.pending_restart == 1
    assert report.fleet_compliant is False


def test_node_manager_rejects_acceptance_for_unregistered_node() -> None:
    manager = NodeManager()

    with pytest.raises(ValueError, match="unknown node"):
        manager.get_desired_release_compliance(
            {"digest": _digest("b"), "sequence": 8},
            accepted_activations={"phantom": _acceptance("b", 8)},
        )
