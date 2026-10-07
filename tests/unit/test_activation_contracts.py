"""Contract tests for the Lil EVY internal activation API."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.services.rag_service.activation_contracts import (
    ActivationCandidateRequest,
    ActivationReceiptResponse,
    ActivationStatusResponse,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def test_candidate_accepts_normalized_server_local_path() -> None:
    request = ActivationCandidateRequest(
        digest=DIGEST_A,
        sequence=7,
        package_path="/var/lib/edge-inference/corpus/incoming/release-7",
    )

    assert request.sequence == 7


@pytest.mark.parametrize(
    "package_path",
    [
        "incoming/release-7",
        "/var/lib/../tmp/release-7",
        "/var/lib//release-7",
        "/var/lib/release-7/.",
        "https://factory.example/release-7",
        "file:///var/lib/release-7",
        r"C:\\incoming\\release-7",
        "/",
    ],
)
def test_candidate_rejects_unsafe_or_non_local_paths(package_path: str) -> None:
    with pytest.raises(ValidationError):
        ActivationCandidateRequest(
            digest=DIGEST_A, sequence=7, package_path=package_path
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("digest", "sha256:ABC"), ("sequence", 0), ("sequence", -1)],
)
def test_candidate_rejects_invalid_identity(field: str, value: object) -> None:
    payload = {
        "digest": DIGEST_A,
        "sequence": 7,
        "package_path": "/srv/lil-evy/incoming/release-7",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        ActivationCandidateRequest.model_validate(payload)


def test_contracts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ActivationCandidateRequest(
            digest=DIGEST_A,
            sequence=7,
            package_path="/srv/lil-evy/incoming/release-7",
            operator_note="activate immediately",
        )


def test_status_accepts_ready_production_and_recovery_states() -> None:
    production = ActivationStatusResponse(
        active_digest=DIGEST_A,
        active_sequence=7,
        sequence_floor=7,
        mode="production",
        state="ACTIVE",
        ready=True,
        reason_code="READY",
    )
    recovery = ActivationStatusResponse(
        active_digest=DIGEST_B,
        active_sequence=4,
        sequence_floor=7,
        mode="recovery",
        state="RECOVERY",
        ready=True,
        reason_code="RECOVERY_ACTIVE",
    )

    assert production.ready is True
    assert recovery.sequence_floor == 7


def test_status_accepts_exact_uninitialized_state() -> None:
    status = ActivationStatusResponse(
        active_digest=None,
        active_sequence=0,
        sequence_floor=0,
        mode="uninitialized",
        state="UNINITIALIZED",
        ready=False,
        reason_code="NO_ACTIVE_CORPUS",
    )

    assert status.active_digest is None


@pytest.mark.parametrize(
    "change",
    [
        {"active_sequence": 8},
        {"active_sequence": 0},
        {"mode": "uninitialized"},
        {"state": "RECOVERY"},
        {"ready": False},
        {"reason_code": "ACTIVATION_FAILED"},
    ],
)
def test_status_rejects_inconsistent_ready_state(change: dict[str, object]) -> None:
    payload = {
        "active_digest": DIGEST_A,
        "active_sequence": 7,
        "sequence_floor": 7,
        "mode": "production",
        "state": "ACTIVE",
        "ready": True,
        "reason_code": "READY",
    }
    payload.update(change)

    with pytest.raises(ValidationError):
        ActivationStatusResponse.model_validate(payload)


def _receipt_payload() -> dict[str, object]:
    return {
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


def test_receipt_is_a_bounded_projection() -> None:
    receipt = ActivationReceiptResponse.model_validate(_receipt_payload())
    serialized = receipt.model_dump(mode="json", by_alias=True)

    assert set(serialized) == {
        "receipt_id",
        "desired",
        "activated",
        "previous_digest",
        "transition",
        "result",
        "reason_code",
        "device_counter",
        "created_at",
    }
    assert serialized["transition"] == {"from": "READY", "to": "ACTIVE"}


@pytest.mark.parametrize(
    "change",
    [
        {"activated": {"digest": DIGEST_B, "sequence": 7}},
        {"transition": {"from": "READY", "to": "REJECTED"}},
        {"device_counter": 0},
        {"created_at": datetime(2026, 10, 6, 12, 30)},
    ],
)
def test_receipt_rejects_inconsistent_or_unbounded_values(
    change: dict[str, object],
) -> None:
    payload = _receipt_payload()
    payload.update(change)

    with pytest.raises(ValidationError):
        ActivationReceiptResponse.model_validate(payload)


def test_failed_receipt_requires_rejected_transition() -> None:
    payload = _receipt_payload()
    payload.update(
        activated={"digest": DIGEST_B, "sequence": 6},
        transition={"from": "INDEXED", "to": "REJECTED"},
        result="failed",
        reason_code="ACTIVATION_FAILED",
    )

    assert ActivationReceiptResponse.model_validate(payload).result.value == "failed"
