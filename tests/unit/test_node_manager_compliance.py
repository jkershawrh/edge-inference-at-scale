"""Strict fleet inventory for activation-managed Lil EVY nodes."""

import time

import pytest
from pydantic import ValidationError

from backend.services.node_manager.main import NodeHeartbeat, NodeManager


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _active(character: str, sequence: int, **overrides):
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


def _manager(*node_ids: str) -> NodeManager:
    manager = NodeManager()
    for node_id in node_ids:
        manager.register_node(node_id, f"http://{node_id}:8000", {})
    return manager


def test_existing_heartbeat_callers_remain_valid_but_are_unknown() -> None:
    manager = _manager("legacy-node")

    manager.heartbeat("legacy-node", {"messages_received": 3})

    node = manager.get_fleet_status()[0]
    assert node["corpus"] == {
        "classification": "unknown",
        "active_digest": None,
        "active_sequence": None,
    }
    inventory = manager.get_fleet_summary()["corpus_inventory"]
    assert inventory["unknown_nodes"] == ["legacy-node"]
    assert inventory["active_nodes"] == []


def test_heartbeat_persists_only_bounded_activation_projection() -> None:
    manager = _manager("field-node")
    heartbeat = NodeHeartbeat(
        node_id="field-node",
        metrics={"messages_received": 9},
        activation=_active("a", 4),
    )

    manager.heartbeat(heartbeat.node_id, heartbeat.metrics, heartbeat.activation)

    stored = manager.nodes["field-node"]["activation"]
    assert stored == _active("a", 4)
    assert set(stored) == {
        "active_digest",
        "active_sequence",
        "sequence_floor",
        "mode",
        "state",
        "ready",
        "reason_code",
    }
    serialized = str(stored)
    assert "/" not in serialized
    assert "content" not in serialized.lower()


@pytest.mark.parametrize(
    "activation",
    [
        _active("a", 1, state="REJECTED"),
        {**_active("a", 1), "package_path": "/var/lib/corpus"},
        _active("a", 1, active_digest="not-a-digest"),
        _active("a", 1, active_sequence=2, sequence_floor=1),
    ],
)
def test_malformed_activation_heartbeat_is_rejected(activation) -> None:
    with pytest.raises(ValidationError):
        NodeHeartbeat(node_id="field-node", activation=activation)


def test_direct_manager_call_also_rejects_malformed_activation() -> None:
    manager = _manager("field-node")

    with pytest.raises(ValidationError):
        manager.heartbeat(
            "field-node",
            {},
            {**_active("a", 1), "manifest_path": "/srv/corpus/manifest.json"},
        )

    assert manager.nodes["field-node"]["activation"] is None


def test_summary_separates_active_recovery_unready_unknown_and_drift() -> None:
    manager = _manager(
        "active-a",
        "active-b",
        "recovery-a",
        "unready-a",
        "unknown-a",
        "offline-a",
    )
    manager.heartbeat("active-a", {}, _active("a", 7))
    manager.heartbeat("active-b", {}, _active("b", 8))
    manager.heartbeat(
        "recovery-a",
        {},
        _active(
            "a",
            6,
            sequence_floor=7,
            mode="recovery",
            state="RECOVERY",
            reason_code="RECOVERY_ACTIVE",
        ),
    )
    manager.heartbeat(
        "unready-a",
        {},
        {
            "active_digest": None,
            "active_sequence": 0,
            "sequence_floor": 0,
            "mode": "uninitialized",
            "state": "UNINITIALIZED",
            "ready": False,
            "reason_code": "NO_ACTIVE_CORPUS",
        },
    )
    manager.heartbeat("unknown-a", {})
    manager.heartbeat("offline-a", {}, _active("a", 7))
    manager.nodes["offline-a"]["last_seen"] = time.time() - 61

    fleet = {node["node_id"]: node for node in manager.get_fleet_status()}
    assert fleet["active-a"]["corpus"]["classification"] == "active"
    assert fleet["recovery-a"]["corpus"]["classification"] == "recovery"
    assert fleet["unready-a"]["corpus"]["classification"] == "unready"
    assert fleet["unknown-a"]["corpus"]["classification"] == "unknown"
    assert fleet["offline-a"]["corpus"]["classification"] == "unknown"

    inventory = manager.get_fleet_summary()["corpus_inventory"]
    assert inventory == {
        "active_nodes": ["active-a", "active-b"],
        "recovery_nodes": ["recovery-a"],
        "unready_nodes": ["unready-a"],
        "unknown_nodes": ["offline-a", "unknown-a"],
        "drift_detected": True,
        "drift_groups": [
            {
                "active_digest": _digest("a"),
                "active_sequence": 6,
                "nodes": ["recovery-a"],
            },
            {
                "active_digest": _digest("a"),
                "active_sequence": 7,
                "nodes": ["active-a"],
            },
            {
                "active_digest": _digest("b"),
                "active_sequence": 8,
                "nodes": ["active-b"],
            },
        ],
    }


def test_same_release_is_one_drift_group() -> None:
    manager = _manager("node-a", "node-b")
    manager.heartbeat("node-a", {}, _active("c", 3))
    manager.heartbeat("node-b", {}, _active("c", 3))

    inventory = manager.get_fleet_summary()["corpus_inventory"]
    assert inventory["drift_detected"] is False
    assert inventory["drift_groups"] == [
        {
            "active_digest": _digest("c"),
            "active_sequence": 3,
            "nodes": ["node-a", "node-b"],
        }
    ]
