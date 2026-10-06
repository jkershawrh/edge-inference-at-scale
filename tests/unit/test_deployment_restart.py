"""Tests for the narrow in-cluster Deployment restart adapter."""
import json
from pathlib import Path

import httpx
import pytest

from backend.services.rag_service.deployment_restart import (
    DeploymentRestartConfigurationError,
    DeploymentRestartError,
    KubernetesDeploymentRestarter,
)


TOKEN = "header.payload.signature"
OPERATION_ID = "11111111-1111-4111-8111-111111111111:restart:1"
DIGEST = "sha256:" + "a" * 64
SEQUENCE = 7


def _response(
    *,
    name="lil-evy-rag",
    namespace="edge-system",
    status=200,
    operation_id=OPERATION_ID,
    digest=DIGEST,
    sequence=SEQUENCE,
):
    return httpx.Response(
        status,
        json={
            "metadata": {
                "name": name,
                "namespace": namespace,
                "generation": 9,
                "resourceVersion": "1234567",
            },
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "lilevy.edge/restart-operation": operation_id,
                            "lilevy.edge/target-digest": digest,
                            "lilevy.edge/target-sequence": str(sequence),
                        }
                    }
                }
            },
        },
    )


def _token(tmp_path: Path, value: str = TOKEN) -> Path:
    path = tmp_path / "token"
    path.write_text(value, encoding="ascii")
    return path


def _adapter(client: httpx.AsyncClient, token_path: Path, **changes):
    values = {
        "namespace": "edge-system",
        "deployment_name": "lil-evy-rag",
        "service_account_token_path": token_path,
    }
    values.update(changes)
    return KubernetesDeploymentRestarter(client, **values)


async def _restart(adapter: KubernetesDeploymentRestarter, **changes):
    values = {
        "operation_id": OPERATION_ID,
        "target_digest": DIGEST,
        "target_sequence": SEQUENCE,
    }
    values.update(changes)
    return await adapter.request_restart(**values)


@pytest.mark.asyncio
async def test_patches_only_configured_deployment_template_annotation(tmp_path: Path):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content)
        return _response()

    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = _adapter(client, _token(tmp_path))
        result = await _restart(adapter)

    assert captured["method"] == "PATCH"
    assert captured["url"] == (
        "https://kubernetes.default.svc/apis/apps/v1/namespaces/"
        "edge-system/deployments/lil-evy-rag"
    )
    assert captured["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert captured["headers"]["content-type"] == (
        "application/strategic-merge-patch+json"
    )
    assert captured["json"] == {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "lilevy.edge/restart-operation": OPERATION_ID,
                        "lilevy.edge/target-digest": DIGEST,
                        "lilevy.edge/target-sequence": "7",
                    }
                }
            }
        }
    }
    assert result.namespace == "edge-system"
    assert result.deployment_name == "lil-evy-rag"
    assert result.operation_id == OPERATION_ID
    assert result.target_digest == DIGEST
    assert result.target_sequence == SEQUENCE
    assert result.generation == 9
    assert result.resource_version == "1234567"


@pytest.mark.asyncio
async def test_rereads_rotated_service_account_token_each_attempt(tmp_path: Path):
    observed = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers["authorization"])
        return _response()

    token_path = _token(tmp_path, "first.token.value")
    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = _adapter(client, token_path)
        await _restart(adapter)
        token_path.write_text("rotated.token.value", encoding="ascii")
        await _restart(adapter)

    assert observed == ["Bearer first.token.value", "Bearer rotated.token.value"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason_code", "retryable"),
    [
        (401, "AUTHORIZATION_REJECTED", False),
        (403, "AUTHORIZATION_REJECTED", False),
        (404, "DEPLOYMENT_NOT_FOUND", False),
        (409, "API_CONFLICT", True),
        (429, "API_UNAVAILABLE", True),
        (503, "API_UNAVAILABLE", True),
        (422, "API_REJECTED", False),
    ],
)
async def test_maps_http_failures_without_exposing_response(
    tmp_path: Path, status: int, reason_code: str, retryable: bool
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="server-secret-diagnostic")

    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(DeploymentRestartError) as raised:
            await _restart(_adapter(client, _token(tmp_path)))

    assert raised.value.reason_code == reason_code
    assert raised.value.retryable is retryable
    assert "server-secret-diagnostic" not in str(raised.value)
    assert TOKEN not in str(raised.value)


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_retryable(tmp_path: Path):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive upstream detail", request=request)

    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(DeploymentRestartError) as raised:
            await _restart(_adapter(client, _token(tmp_path)))

    assert raised.value.reason_code == "API_TIMEOUT"
    assert raised.value.retryable is True
    assert "sensitive" not in str(raised.value)


@pytest.mark.asyncio
async def test_caps_response_before_json_parsing(tmp_path: Path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1025)

    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(DeploymentRestartError) as raised:
            await _restart(
                _adapter(client, _token(tmp_path), maximum_response_bytes=1024)
            )

    assert raised.value.reason_code == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        _response(name="some-other-deployment"),
        _response(namespace="other-namespace"),
        _response(operation_id="some-other-operation"),
        _response(digest="sha256:" + "b" * 64),
        _response(sequence=8),
        httpx.Response(
            200,
            json={
                "metadata": {
                    "name": "lil-evy-rag",
                    "namespace": "edge-system",
                    "generation": True,
                    "resourceVersion": "123",
                },
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "lilevy.edge/restart-operation": OPERATION_ID,
                                "lilevy.edge/target-digest": DIGEST,
                                "lilevy.edge/target-sequence": "7",
                            }
                        }
                    }
                },
            },
        ),
    ],
)
async def test_rejects_invalid_or_wrong_target_success_response(
    tmp_path: Path, response: httpx.Response
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(DeploymentRestartError) as raised:
            await _restart(_adapter(client, _token(tmp_path)))

    assert raised.value.reason_code == "INVALID_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "token with space", "token\n", "t\N{SNOWMAN}"])
async def test_rejects_invalid_credentials_without_sending_request(
    tmp_path: Path, token: str
):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response()

    token_path = tmp_path / "token"
    token_path.write_text(token, encoding="utf-8")
    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(DeploymentRestartError) as raised:
            await _restart(_adapter(client, token_path))

    assert raised.value.reason_code == "CREDENTIAL_INVALID"
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"namespace": "Edge_System"},
        {"namespace": "x" * 64},
        {"deployment_name": "../another"},
        {"deployment_name": "UPPER"},
        {"deployment_name": "x" * 254},
        {"service_account_token_path": "relative/token"},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"maximum_response_bytes": 100},
    ],
)
async def test_configuration_strictly_scopes_target_and_bounds(
    changes, tmp_path: Path
):
    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc"
    ) as client:
        with pytest.raises(DeploymentRestartConfigurationError):
            _adapter(client, _token(tmp_path), **changes)


@pytest.mark.asyncio
async def test_rejects_client_targeting_any_other_api_origin(tmp_path: Path):
    async with httpx.AsyncClient(base_url="https://attacker.invalid") as client:
        with pytest.raises(DeploymentRestartConfigurationError):
            _adapter(client, _token(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("operation_id", "contains a space"),
        ("operation_id", "x" * 129),
        ("target_digest", "sha256:not-a-digest"),
        ("target_digest", "sha256:" + "A" * 64),
        ("target_sequence", 0),
        ("target_sequence", -1),
        ("target_sequence", True),
        ("target_sequence", 2**63),
    ],
)
async def test_rejects_invalid_rollout_intent_before_sending_request(
    tmp_path: Path, change: str, value
):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response()

    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = _adapter(client, _token(tmp_path))
        with pytest.raises(DeploymentRestartError) as raised:
            await _restart(adapter, **{change: value})

    assert raised.value.reason_code == "INVALID_ROLLOUT_INTENT"
    assert raised.value.retryable is False
    assert calls == 0


@pytest.mark.asyncio
async def test_replay_uses_identical_idempotent_patch(tmp_path: Path):
    bodies = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return _response()

    async with httpx.AsyncClient(
        base_url="https://kubernetes.default.svc",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = _adapter(client, _token(tmp_path))
        first = await _restart(adapter)
        second = await _restart(adapter)

    assert bodies[0] == bodies[1]
    assert first == second
