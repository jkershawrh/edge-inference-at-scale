"""HTTP boundary tests for the standalone Lil EVY activation operator."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.services.rag_service.activation_contracts import ActivationReceiptResponse
from backend.services.rag_service.activation_control import (
    ActivationAuthenticationError,
    ActivationPathError,
)
from backend.services.rag_service.activation_operator import create_activation_operator_app


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
TOKEN = "operator-token-with-at-least-32-bytes"


def _receipt(*, success: bool = True) -> ActivationReceiptResponse:
    return ActivationReceiptResponse.model_validate(
        {
            "receipt_id": "89a8bb70-bf06-4a1e-89a6-306516e93d8f",
            "desired": {"digest": DIGEST_A, "sequence": 7},
            "activated": {"digest": DIGEST_A if success else DIGEST_B, "sequence": 7 if success else 6},
            "previous_digest": DIGEST_B,
            "transition": {"from": "READY", "to": "ACTIVE" if success else "REJECTED"},
            "result": "success" if success else "rejected",
            "reason_code": "ACTIVATED" if success else "SEQUENCE_ROLLBACK",
            "device_counter": 12,
            "created_at": "2026-10-06T12:30:00Z",
        }
    )


class FakeControl:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, str]] = []

    def activate(self, request: object, authorization: str) -> ActivationReceiptResponse:
        self.calls.append((request, authorization))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _payload(path: str = "/srv/lil-evy/intake/release-7") -> dict[str, object]:
    return {"digest": DIGEST_A, "sequence": 7, "package_path": path}


def _client(control: FakeControl, maximum: int = 8192) -> TestClient:
    return TestClient(
        create_activation_operator_app(control, max_request_bytes=maximum)  # type: ignore[arg-type]
    )


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def test_health_reveals_no_secret_or_path() -> None:
    response = _client(FakeControl(_receipt())).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "lil-evy-activation-operator",
        "status": "ready",
        "version": "1",
    }
    serialized = response.text
    assert TOKEN not in serialized
    assert "/srv/" not in serialized


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": TOKEN}, {"Authorization": "Basic " + TOKEN}, {"Authorization": "Bearer "}],
)
def test_missing_or_malformed_authorization_maps_to_401(headers: dict[str, str]) -> None:
    control = FakeControl(_receipt())
    response = _client(control).post("/v1/activation", json=_payload(), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["reason_code"] == "INVALID_AUTHORIZATION"
    assert control.calls == []


def test_rejected_credential_maps_to_bounded_403() -> None:
    control = FakeControl(ActivationAuthenticationError("secret detail"))
    response = _client(control).post(
        "/v1/activation", json=_payload(), headers=_headers("wrong-token-with-at-least-32-bytes")
    )

    assert response.status_code == 403
    assert response.json() == {
        "error": "forbidden",
        "reason_code": "CREDENTIAL_REJECTED",
        "message": "The activation credential was not accepted.",
        "receipt": None,
    }
    assert "secret detail" not in response.text


def test_duplicate_authorization_headers_are_rejected() -> None:
    control = FakeControl(_receipt())
    response = _client(control).post(
        "/v1/activation",
        json=_payload(),
        headers=[
            ("Authorization", "Bearer " + TOKEN),
            ("Authorization", "Bearer another-token-with-at-least-32-bytes"),
        ],
    )

    assert response.status_code == 401
    assert control.calls == []


def test_success_is_202_and_explicitly_requires_runtime_reconciliation() -> None:
    control = FakeControl(_receipt())
    response = _client(control).post(
        "/v1/activation", json=_payload(), headers=_headers()
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["live_reload_performed"] is False
    assert payload["restart_or_reconciliation_required"] is True
    assert payload["receipt"]["result"] == "success"
    assert control.calls[0][1] == "Bearer " + TOKEN


def test_activation_rejection_maps_to_409_with_only_bounded_receipt() -> None:
    response = _client(FakeControl(_receipt(success=False))).post(
        "/v1/activation", json=_payload(), headers=_headers()
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["reason_code"] == "SEQUENCE_ROLLBACK"
    assert payload["receipt"]["result"] == "rejected"
    assert set(payload["receipt"]) == {
        "receipt_id", "desired", "activated", "previous_digest", "transition",
        "result", "reason_code", "device_counter", "created_at",
    }


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not-json", "application/json"),
        (b"[]", "application/json"),
        (b'{"digest":"unexpected"}', "application/json"),
        (b"{}", "text/plain"),
    ],
)
def test_invalid_requests_map_to_bounded_422(body: bytes, content_type: str) -> None:
    control = FakeControl(_receipt())
    response = _client(control).post(
        "/v1/activation",
        content=body,
        headers={**_headers(), "Content-Type": content_type},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert control.calls == []


def test_unknown_request_field_maps_to_422() -> None:
    payload = _payload()
    payload["operator_note"] = "do it now"
    response = _client(FakeControl(_receipt())).post(
        "/v1/activation", json=payload, headers=_headers()
    )

    assert response.status_code == 422


def test_package_confinement_failure_maps_to_422_without_detail_leak() -> None:
    control = FakeControl(ActivationPathError("/secret/root escaped"))
    response = _client(control).post(
        "/v1/activation", json=_payload(), headers=_headers()
    )

    assert response.status_code == 422
    assert response.json()["reason_code"] == "INVALID_PACKAGE_PATH"
    assert "/secret/root" not in response.text


def test_request_body_is_bounded_before_parsing() -> None:
    control = FakeControl(_receipt())
    response = _client(control, maximum=256).post(
        "/v1/activation",
        content=b"{" + b"x" * 300 + b"}",
        headers={**_headers(), "Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["reason_code"] == "REQUEST_TOO_LARGE"
    assert control.calls == []


def test_unexpected_failure_maps_to_generic_bounded_500() -> None:
    response = _client(FakeControl(RuntimeError("sensitive runtime detail"))).post(
        "/v1/activation", json=_payload(), headers=_headers()
    )

    assert response.status_code == 500
    assert response.json()["reason_code"] == "ACTIVATION_INTERNAL_ERROR"
    assert "sensitive runtime detail" not in response.text
