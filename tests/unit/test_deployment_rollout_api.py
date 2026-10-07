"""Tests for the internal deployment-rollout HTTP boundary."""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from backend.services.rag_service.deployment_rollout import RolloutPhase, RolloutReason
from backend.services.rag_service.deployment_rollout_api import (
    MAXIMUM_REQUEST_BYTES,
    create_deployment_rollout_app,
)
from tests.unit.test_deployment_rollout import (
    Activation,
    MemoryStore,
    Status,
    Workload,
    _controller,
)


TOKEN = "rollout-operator-token-with-32-bytes"


def _client(controller=None, token: str = TOKEN) -> TestClient:
    return TestClient(
        create_deployment_rollout_app(
            controller or _controller(), bearer_credential=token
        )
    )


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def test_health_is_bounded_and_schema_endpoints_are_disabled():
    client = _client()

    assert client.get("/health").json() == {
        "service": "lil-evy-deployment-rollout",
        "status": "ready",
        "version": "1",
    }
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert TOKEN not in client.get("/health").text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": TOKEN},
        {"Authorization": "Basic " + TOKEN},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer token with spaces"},
        {"Authorization": "Bearer " + "x" * 513},
    ],
)
def test_missing_malformed_or_oversized_authorization_is_bounded_401(headers):
    response = _client().get("/v1/rollout", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": "unauthorized",
        "reason_code": "INVALID_AUTHORIZATION",
        "message": "A single valid Bearer authorization header is required.",
    }


def test_duplicate_authorization_headers_are_rejected_without_controller_use():
    store = MemoryStore()
    response = _client(_controller(store=store)).get(
        "/v1/rollout",
        headers=[
            ("Authorization", "Bearer " + TOKEN),
            ("Authorization", "Bearer " + TOKEN),
        ],
    )

    assert response.status_code == 401
    assert store.value is None


def test_wrong_well_formed_credential_is_bounded_403():
    response = _client().get(
        "/v1/rollout",
        headers=_headers("wrong-rollout-token-with-32-bytes"),
    )

    assert response.status_code == 403
    assert response.json()["reason_code"] == "CREDENTIAL_REJECTED"
    assert "wrong-rollout" not in response.text


@pytest.mark.parametrize(
    "token",
    ["short", "x" * 513, "x" * 31 + " ", "tök" * 20],
)
def test_factory_rejects_unsafe_credentials(token: str):
    with pytest.raises(ValueError, match="bearer_credential"):
        create_deployment_rollout_app(_controller(), bearer_credential=token)


def test_get_is_read_only_and_returns_only_bounded_durable_projection():
    store, activation, workload, status = MemoryStore(), Activation(), Workload(), Status()
    controller = _controller(
        store=store, activation=activation, workload=workload, status=status
    )

    first = _client(controller).get("/v1/rollout", headers=_headers())
    second = _client(controller).get("/v1/rollout", headers=_headers())

    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["phase"] == RolloutPhase.ACTIVATING.value
    assert first.json()["reason_code"] == RolloutReason.ACTIVATION_REQUIRED.value
    assert set(first.json()) == {
        "rollout_id",
        "target",
        "phase",
        "reason_code",
        "activation_attempts",
        "restart_attempts",
        "status_polls",
        "observed_digest",
        "observed_sequence",
        "complete",
    }
    assert activation.calls == []
    assert workload.calls == []
    assert status.calls == 0


def test_each_post_advances_at_most_one_external_operation():
    activation, workload, status = Activation(), Workload(), Status()
    client = _client(
        _controller(activation=activation, workload=workload, status=status)
    )

    activated = client.post("/v1/rollout/advance", headers=_headers())
    assert activated.status_code == 200
    assert activated.json()["phase"] == RolloutPhase.RESTARTING.value
    assert len(activation.calls) == 1
    assert workload.calls == []
    assert status.calls == 0

    restarted = client.post("/v1/rollout/advance", headers=_headers())
    assert restarted.json()["phase"] == RolloutPhase.OBSERVING.value
    assert len(activation.calls) == 1
    assert len(workload.calls) == 1
    assert status.calls == 0

    verified = client.post("/v1/rollout/advance", headers=_headers())
    assert verified.json()["phase"] == RolloutPhase.VERIFIED.value
    assert len(workload.calls) == 1
    assert status.calls == 1


def test_post_requires_empty_bounded_body_before_controller_use():
    activation = Activation()
    client = _client(_controller(activation=activation))

    nonempty = client.post(
        "/v1/rollout/advance", content=b"{}", headers=_headers()
    )
    oversized = client.post(
        "/v1/rollout/advance",
        content=b"x" * (MAXIMUM_REQUEST_BYTES + 1),
        headers=_headers(),
    )

    assert nonempty.status_code == 422
    assert nonempty.json()["reason_code"] == "INVALID_REQUEST"
    assert oversized.status_code == 413
    assert oversized.json()["reason_code"] == "REQUEST_TOO_LARGE"
    assert activation.calls == []


def test_get_requires_empty_bounded_body_before_state_read():
    store = MemoryStore()
    client = _client(_controller(store=store))

    nonempty = client.request(
        "GET", "/v1/rollout", content=b"{}", headers=_headers()
    )
    oversized = client.request(
        "GET",
        "/v1/rollout",
        content=b"x" * (MAXIMUM_REQUEST_BYTES + 1),
        headers=_headers(),
    )

    assert nonempty.status_code == 422
    assert oversized.status_code == 413
    assert store.value is None


def test_dependency_failure_is_generic_503_without_detail_leak():
    activation = Activation()
    activation.error = RuntimeError("secret remote hostname")
    response = _client(_controller(activation=activation)).post(
        "/v1/rollout/advance", headers=_headers()
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": "dependency_unavailable",
        "reason_code": "ROLLOUT_DEPENDENCY_UNAVAILABLE",
        "message": "A rollout dependency is temporarily unavailable.",
    }
    assert "secret remote hostname" not in response.text


def test_corrupt_state_is_generic_409_without_detail_leak():
    store = MemoryStore()
    controller = _controller(store=store)
    controller.current_result()
    store.value["secret_path"] = "/private/operator/state"

    response = _client(controller).get("/v1/rollout", headers=_headers())

    assert response.status_code == 409
    assert response.json()["reason_code"] == "ROLLOUT_STATE_UNAVAILABLE"
    assert "/private/operator/state" not in response.text
