from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from backend.services.rag_service.deployment_rollout_runtime import (
    RolloutRuntimeConfigurationError,
    RolloutRuntimeSettings,
    create_deployment_rollout_from_environment,
)


ACTIVATION_ORIGIN = "http://activation-operator.edge-system.svc:8014"
RAG_ORIGIN = "http://rag-service.edge-system.svc:8004"
DIGEST = "sha256:" + "a" * 64
API_TOKEN = "rollout-api-token-value-1234567890"
ACTIVATION_TOKEN = "activation-token-value-123456789012"


def _environment(tmp_path: Path) -> dict[str, str]:
    files = {
        "ROLLOUT_API_TOKEN_PATH": ("rollout-token", API_TOKEN),
        "ACTIVATION_BEARER_TOKEN_PATH": ("activation-token", ACTIVATION_TOKEN),
        "SERVICE_ACCOUNT_TOKEN_PATH": ("service-account-token", "service-token"),
        "SERVICE_ACCOUNT_NAMESPACE_PATH": ("namespace", "edge-system\n"),
        "SERVICE_ACCOUNT_CA_PATH": ("ca.crt", "test-ca"),
    }
    values: dict[str, str] = {}
    for variable, (name, content) in files.items():
        path = tmp_path / name
        path.write_text(content, encoding="ascii")
        values[variable] = str(path)
    values.update(
        {
            "ACTIVATION_INTERNAL_ORIGIN": ACTIVATION_ORIGIN,
            "RAG_INTERNAL_ORIGIN": RAG_ORIGIN,
            "ROLLOUT_CANDIDATE_PATH": "/data/activation-intake/event-7",
            "ROLLOUT_CANDIDATE_DIGEST": DIGEST,
            "ROLLOUT_CANDIDATE_SEQUENCE": "7",
            "ROLLOUT_STATE_PATH": str(tmp_path / "state" / "rollout.json"),
            "RAG_DEPLOYMENT_NAME": "lil-evy-rag-service",
            "ROLLOUT_MAX_ACTIVATION_ATTEMPTS": "3",
            "ROLLOUT_MAX_RESTART_ATTEMPTS": "4",
            "ROLLOUT_MAX_STATUS_POLLS": "20",
            "ROLLOUT_TIMEOUT_SECONDS": "300",
            "ROLLOUT_DEPENDENCY_TIMEOUT_SECONDS": "7.5",
            "ROLLOUT_WORKER_POLL_SECONDS": "1",
        }
    )
    return values


def _accepted() -> dict:
    return {
        "receipt": {
            "receipt_id": str(UUID("22222222-2222-4222-8222-222222222222")),
            "desired": {"digest": DIGEST, "sequence": 7},
            "activated": {"digest": DIGEST, "sequence": 7},
            "previous_digest": None,
            "transition": {"from": "UNINITIALIZED", "to": "ACTIVE"},
            "result": "success",
            "reason_code": "ACTIVATED",
            "device_counter": 1,
            "created_at": datetime(2026, 10, 6, tzinfo=timezone.utc).isoformat(),
        },
        "live_reload_performed": False,
        "restart_or_reconciliation_required": True,
    }


def test_settings_are_explicit_and_bounded(tmp_path: Path) -> None:
    settings = RolloutRuntimeSettings(_environment(tmp_path))

    assert settings.namespace == "edge-system"
    assert settings.candidate.digest == DIGEST
    assert settings.configuration.maximum_restart_attempts == 4
    assert settings.dependency_timeout_seconds == 7.5
    assert settings.worker_poll_seconds == 1
    assert settings.rollout_api_credential == API_TOKEN


def test_settings_accept_pinned_same_pod_loopback_origins(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["ACTIVATION_INTERNAL_ORIGIN"] = "http://127.0.0.1:8014"
    environment["RAG_INTERNAL_ORIGIN"] = "http://127.0.0.1:8004"

    settings = RolloutRuntimeSettings(environment)

    assert settings.activation_origin == "http://127.0.0.1:8014"
    assert settings.rag_origin == "http://127.0.0.1:8004"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ROLLOUT_CANDIDATE_SEQUENCE", "0"),
        ("ROLLOUT_MAX_ACTIVATION_ATTEMPTS", "101"),
        ("ROLLOUT_MAX_STATUS_POLLS", "0"),
        ("ROLLOUT_TIMEOUT_SECONDS", "0"),
        ("ROLLOUT_DEPENDENCY_TIMEOUT_SECONDS", "60.1"),
        ("ROLLOUT_WORKER_POLL_SECONDS", "0.9"),
        ("ROLLOUT_WORKER_POLL_SECONDS", "61"),
        ("ROLLOUT_STATE_PATH", "relative/state.json"),
        ("ACTIVATION_INTERNAL_ORIGIN", "http://localhost:8014"),
        ("RAG_INTERNAL_ORIGIN", "http://rag-service.edge-system.svc:8004/path"),
        ("RAG_DEPLOYMENT_NAME", "Bad_Name"),
    ],
)
def test_rejects_invalid_or_unbounded_environment(
    tmp_path: Path, name: str, value: str
) -> None:
    environment = _environment(tmp_path)
    environment[name] = value

    with pytest.raises(RolloutRuntimeConfigurationError):
        RolloutRuntimeSettings(environment)


def test_rejects_missing_environment_without_naming_secret(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment.pop("ROLLOUT_API_TOKEN_PATH")

    with pytest.raises(
        RolloutRuntimeConfigurationError,
        match="rollout runtime environment configuration is invalid",
    ) as error:
        create_deployment_rollout_from_environment(environment)

    assert API_TOKEN not in str(error.value)


def test_accepts_contained_projected_files_and_rejects_escape(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    mount = tmp_path / "projected"
    version = mount / "..2026_10_06"
    version.mkdir(parents=True)
    target = version / "token"
    target.write_text(API_TOKEN, encoding="ascii")
    (mount / "rollout-token").symlink_to(target)
    environment["ROLLOUT_API_TOKEN_PATH"] = str(mount / "rollout-token")

    assert RolloutRuntimeSettings(environment).rollout_api_credential == API_TOKEN

    outside = tmp_path / "outside-token"
    outside.write_text(API_TOKEN, encoding="ascii")
    (mount / "rollout-token").unlink()
    (mount / "rollout-token").symlink_to(outside)
    with pytest.raises(RolloutRuntimeConfigurationError):
        RolloutRuntimeSettings(environment)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("ROLLOUT_API_TOKEN_PATH", "ACTIVATION_BEARER_TOKEN_PATH"),
        ("ROLLOUT_API_TOKEN_PATH", "SERVICE_ACCOUNT_TOKEN_PATH"),
        ("ACTIVATION_BEARER_TOKEN_PATH", "SERVICE_ACCOUNT_TOKEN_PATH"),
    ],
)
def test_credential_files_and_values_must_be_distinct(
    tmp_path: Path, left: str, right: str
) -> None:
    same_file = _environment(tmp_path)
    same_file[right] = same_file[left]
    with pytest.raises(RolloutRuntimeConfigurationError):
        RolloutRuntimeSettings(same_file)

    same_value = _environment(tmp_path)
    Path(same_value[right]).write_text(
        Path(same_value[left]).read_text(encoding="ascii"), encoding="ascii"
    )
    with pytest.raises(RolloutRuntimeConfigurationError):
        RolloutRuntimeSettings(same_value)


@pytest.mark.asyncio
async def test_lifespan_runs_complete_rollout_with_pinned_clients_and_closes_them(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    client_arguments: list[tuple[str, bool | str]] = []
    clients: list[httpx.AsyncClient] = []
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/activation":
            return httpx.Response(202, json=_accepted())
        if request.url.path.startswith("/apis/apps/v1/"):
            patch = json.loads(request.content)
            annotations = patch["spec"]["template"]["metadata"]["annotations"]
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "name": "lil-evy-rag-service",
                        "namespace": "edge-system",
                        "generation": 2,
                        "resourceVersion": "123",
                    },
                    "spec": {"template": {"metadata": {"annotations": annotations}}},
                },
            )
        if request.url.path == "/activation/status":
            return httpx.Response(
                200,
                json={
                    "active_digest": DIGEST,
                    "active_sequence": 7,
                    "sequence_floor": 7,
                    "mode": "production",
                    "state": "ACTIVE",
                    "ready": True,
                    "reason_code": "READY",
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    def client_factory(*, base_url: str, verify: bool | str) -> httpx.AsyncClient:
        client_arguments.append((base_url, verify))
        client = httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handler)
        )
        clients.append(client)
        return client

    app = create_deployment_rollout_from_environment(
        environment, client_factory=client_factory, sleep=lambda _: asyncio.sleep(0)
    )
    async with app.router.lifespan_context(app):
        for _ in range(100):
            await asyncio.sleep(0)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as api:
                health = (await api.get("/health")).json()
                if health["status"] == "complete":
                    rollout = await api.get(
                        "/v1/rollout",
                        headers={"Authorization": "Bearer " + API_TOKEN},
                    )
                    break
        else:
            pytest.fail("autonomous rollout did not complete")

        assert rollout.json()["phase"] == "verified"
        assert rollout.json()["complete"] is True

    assert [value[0] for value in client_arguments] == [
        ACTIVATION_ORIGIN,
        RAG_ORIGIN,
        "https://kubernetes.default.svc",
    ]
    assert client_arguments[0][1] is True
    assert client_arguments[1][1] is True
    assert client_arguments[2][1] == str(
        Path(environment["SERVICE_ACCOUNT_CA_PATH"]).resolve()
    )
    assert all(client.is_closed for client in clients)
    assert [request.url.path for request in requests] == [
        "/v1/activation",
        "/apis/apps/v1/namespaces/edge-system/deployments/lil-evy-rag-service",
        "/activation/status",
    ]
    assert requests[0].headers["authorization"] == "Bearer " + ACTIVATION_TOKEN
    assert requests[1].headers["authorization"] == "Bearer service-token"


@pytest.mark.asyncio
async def test_worker_marks_terminal_failure_without_leaking_dependency_body(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment["ROLLOUT_MAX_ACTIVATION_ATTEMPTS"] = "1"

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="sensitive upstream diagnostic")

    def client_factory(*, base_url: str, verify: bool | str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handler)
        )

    app = create_deployment_rollout_from_environment(
        environment, client_factory=client_factory, sleep=lambda _: asyncio.sleep(0)
    )
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            response = await api.get("/health")

    assert response.json() == {
        "service": "lil-evy-deployment-rollout",
        "status": "terminal_failure",
        "version": "1",
    }
    assert "sensitive" not in response.text


@pytest.mark.asyncio
async def test_shutdown_cancels_waiting_worker_and_closes_every_client(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    waiting = asyncio.Event()
    clients: list[httpx.AsyncClient] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    def client_factory(*, base_url: str, verify: bool | str) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handler)
        )
        clients.append(client)
        return client

    async def blocked_sleep(_: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    app = create_deployment_rollout_from_environment(
        environment, client_factory=client_factory, sleep=blocked_sleep
    )
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(waiting.wait(), timeout=1)

    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_partial_client_construction_failure_closes_created_client(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    clients: list[httpx.AsyncClient] = []

    def client_factory(*, base_url: str, verify: bool | str) -> httpx.AsyncClient:
        if clients:
            raise RuntimeError("client construction failed")
        client = httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(lambda _: httpx.Response(500))
        )
        clients.append(client)
        return client

    app = create_deployment_rollout_from_environment(
        environment, client_factory=client_factory
    )
    with pytest.raises(
        RolloutRuntimeConfigurationError,
        match="rollout runtime could not be initialized",
    ) as error:
        async with app.router.lifespan_context(app):
            pass

    assert clients[0].is_closed
    assert "client construction failed" not in str(error.value)
