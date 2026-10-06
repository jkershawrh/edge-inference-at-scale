"""Security-boundary tests for the Lil EVY activation control component."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.rag_service.activation import ActivationFailed
from backend.services.rag_service.activation_contracts import ActivationCandidateRequest
from backend.services.rag_service.activation_control import (
    ActivationAuthenticationError,
    ActivationControl,
    ActivationControlConfigurationError,
    ActivationPathError,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
TOKEN = "operator-token-with-at-least-32-bytes"


class RecordingManager:
    def __init__(self, receipt: object, error: bool = False) -> None:
        self.receipt = receipt
        self.error = error
        self.candidates: list[object] = []

    def activate(self, candidate: object) -> object:
        self.candidates.append(candidate)
        if self.error:
            raise ActivationFailed("sensitive internal detail", self.receipt)
        return self.receipt


def _receipt(**overrides: object) -> object:
    values = {
        "receipt_id": "89a8bb70-bf06-4a1e-89a6-306516e93d8f",
        "desired_digest": DIGEST_A,
        "desired_sequence": 7,
        "activated_digest": DIGEST_A,
        "activated_sequence": 7,
        "previous_digest": DIGEST_B,
        "transition_from": "READY",
        "transition_to": "ACTIVE",
        "result": "success",
        "reason_code": "ACTIVATED",
        "device_counter": 12,
        "created_at": "2026-10-06T12:30:00Z",
        # These deliberately sensitive/unbounded values must not be projected.
        "verification": {"debug": "internal"},
        "runtime": {"host_path": "/private/runtime"},
        "signature": {"value": "secret-adjacent"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(package: Path) -> ActivationCandidateRequest:
    return ActivationCandidateRequest(
        digest=DIGEST_A,
        sequence=7,
        package_path=str(package),
    )


def _control(tmp_path: Path, manager: RecordingManager) -> tuple[ActivationControl, Path]:
    intake = tmp_path / "intake"
    intake.mkdir()
    return (
        ActivationControl(manager, bearer_credential=TOKEN, intake_root=intake),
        intake,
    )


@pytest.mark.parametrize("credential", [None, "", "short-token", "x" * 513, "x" * 31 + " "])
def test_configuration_fails_closed_without_valid_credential(
    tmp_path: Path, credential: str | None
) -> None:
    intake = tmp_path / "intake"
    intake.mkdir()

    with pytest.raises(ActivationControlConfigurationError):
        ActivationControl(
            RecordingManager(_receipt()),
            bearer_credential=credential,
            intake_root=intake,
        )


def test_configuration_rejects_missing_relative_or_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    for root in (tmp_path / "missing", Path("relative-intake"), linked):
        with pytest.raises(ActivationControlConfigurationError):
            ActivationControl(
                RecordingManager(_receipt()), bearer_credential=TOKEN, intake_root=root
            )


@pytest.mark.parametrize(
    "authorization",
    [None, "", TOKEN, "Basic " + TOKEN, "Bearer wrong-token-with-at-least-32-bytes"],
)
def test_authentication_fails_before_filesystem_or_manager_work(
    tmp_path: Path, authorization: str | None
) -> None:
    manager = RecordingManager(_receipt())
    control, intake = _control(tmp_path, manager)
    missing = intake / "missing"

    with pytest.raises(ActivationAuthenticationError):
        control.activate(_request(missing), authorization)

    assert manager.candidates == []


def test_valid_request_invokes_manager_with_confined_release_candidate(tmp_path: Path) -> None:
    manager = RecordingManager(_receipt())
    control, intake = _control(tmp_path, manager)
    package = intake / "event-a" / "release-7"
    package.mkdir(parents=True)

    response = control.activate(_request(package), "Bearer " + TOKEN)

    assert len(manager.candidates) == 1
    candidate = manager.candidates[0]
    assert candidate.digest == DIGEST_A
    assert candidate.sequence == 7
    assert candidate.package_path == package.resolve()
    assert response.result.value == "success"


def test_response_projects_only_bounded_receipt_fields(tmp_path: Path) -> None:
    manager = RecordingManager(_receipt())
    control, intake = _control(tmp_path, manager)
    package = intake / "release-7"
    package.mkdir()

    response = control.activate(_request(package), "Bearer " + TOKEN)
    payload = response.model_dump(mode="json", by_alias=True)

    assert "verification" not in payload
    assert "runtime" not in payload
    assert "signature" not in payload
    assert set(payload) == {
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


def test_expected_manager_failure_returns_only_its_bounded_receipt(tmp_path: Path) -> None:
    receipt = _receipt(
        activated_digest=DIGEST_B,
        activated_sequence=6,
        transition_from="INDEXED",
        transition_to="REJECTED",
        result="failed",
        reason_code="ACTIVATION_FAILED",
    )
    manager = RecordingManager(receipt, error=True)
    control, intake = _control(tmp_path, manager)
    package = intake / "release-7"
    package.mkdir()

    response = control.activate(_request(package), "Bearer " + TOKEN)

    assert response.result.value == "failed"
    assert response.reason_code == "ACTIVATION_FAILED"
    assert "sensitive internal detail" not in response.model_dump_json()


def test_path_outside_intake_root_is_rejected_before_manager(tmp_path: Path) -> None:
    manager = RecordingManager(_receipt())
    control, _ = _control(tmp_path, manager)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ActivationPathError):
        control.activate(_request(outside), "Bearer " + TOKEN)

    assert manager.candidates == []


def test_missing_package_is_rejected_before_manager(tmp_path: Path) -> None:
    manager = RecordingManager(_receipt())
    control, intake = _control(tmp_path, manager)

    with pytest.raises(ActivationPathError):
        control.activate(_request(intake / "missing"), "Bearer " + TOKEN)

    assert manager.candidates == []


def test_symlink_component_is_rejected_even_when_target_stays_inside_root(
    tmp_path: Path,
) -> None:
    manager = RecordingManager(_receipt())
    control, intake = _control(tmp_path, manager)
    actual = intake / "actual"
    actual.mkdir()
    linked = intake / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ActivationPathError):
        control.activate(_request(linked), "Bearer " + TOKEN)

    assert manager.candidates == []


def test_symlink_escape_is_rejected_before_manager(tmp_path: Path) -> None:
    manager = RecordingManager(_receipt())
    control, intake = _control(tmp_path, manager)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = intake / "linked-outside"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ActivationPathError):
        control.activate(_request(linked), "Bearer " + TOKEN)

    assert manager.candidates == []
