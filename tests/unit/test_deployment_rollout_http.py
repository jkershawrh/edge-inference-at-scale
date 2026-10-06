"""Tests for bounded rollout activation and status HTTP ports."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from backend.services.rag_service.activation_contracts import ActivationCandidateRequest
from backend.services.rag_service.deployment_rollout_http import (
    HttpActivationPort,
    HttpStatusPort,
    RolloutHttpConfigurationError,
    RolloutHttpError,
)


ACTIVATION_ORIGIN = "http://activation-operator.edge-system.svc:8014"
STATUS_ORIGIN = "http://rag-service.edge-system.svc:8004"
TOKEN = "a-secure-rollout-token-that-is-long-enough"
OPERATION_ID = "11111111-1111-4111-8111-111111111111:activate"
DIGEST = "sha256:" + "a" * 64
OLD_DIGEST = "sha256:" + "b" * 64


def _token(tmp_path: Path, value: str = TOKEN) -> Path:
    path = tmp_path / "bearer-token"
    path.write_text(value, encoding="utf-8")
    return path


def _candidate(path: str = "/data/activation-intake/release") -> ActivationCandidateRequest:
    return ActivationCandidateRequest(digest=DIGEST, sequence=7, package_path=path)


def _accepted(*, digest: str = DIGEST, sequence: int = 7) -> dict:
    return {
        "receipt": {
            "receipt_id": str(UUID("22222222-2222-4222-8222-222222222222")),
            "desired": {"digest": digest, "sequence": sequence},
            "activated": {"digest": digest, "sequence": sequence},
            "previous_digest": OLD_DIGEST,
            "transition": {"from": "ACTIVE", "to": "ACTIVE"},
            "result": "success",
            "reason_code": "ACTIVATED",
            "device_counter": 8,
            "created_at": datetime(2026, 10, 6, 12, tzinfo=timezone.utc).isoformat(),
        },
        "live_reload_performed": False,
        "restart_or_reconciliation_required": True,
    }


def _status(**changes) -> dict:
    value = {
        "active_digest": DIGEST,
        "active_sequence": 7,
        "sequence_floor": 7,
        "mode": "production",
        "state": "ACTIVE",
        "ready": True,
        "reason_code": "READY",
    }
    value.update(changes)
    return value


def _activation(client: httpx.AsyncClient, token_path: Path, **changes):
    values = {
        "internal_origin": ACTIVATION_ORIGIN,
        "bearer_token_path": token_path,
    }
    values.update(changes)
    return HttpActivationPort(client, **values)


def _status_port(client: httpx.AsyncClient, token_path: Path, **changes):
    values = {
        "internal_origin": STATUS_ORIGIN,
        "bearer_token_path": token_path,
    }
    values.update(changes)
    return HttpStatusPort(client, **values)


@pytest.mark.asyncio
async def test_activation_posts_exact_candidate_with_auth_and_idempotency(tmp_path: Path):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content)
        return httpx.Response(202, json=_accepted())

    async with httpx.AsyncClient(
        base_url=ACTIVATION_ORIGIN,
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        accepted = await _activation(client, _token(tmp_path)).activate(
            _candidate(), operation_id=OPERATION_ID
        )

    assert captured["method"] == "POST"
    assert captured["url"] == f"{ACTIVATION_ORIGIN}/v1/activation"
    assert captured["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert captured["headers"]["idempotency-key"] == OPERATION_ID
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["json"] == _candidate().model_dump(mode="json")
    assert accepted.receipt.desired.digest == DIGEST
    assert accepted.receipt.desired.sequence == 7


@pytest.mark.asyncio
async def test_status_gets_only_fixed_path_with_auth(tmp_path: Path):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_status())

    async with httpx.AsyncClient(
        base_url=STATUS_ORIGIN, transport=httpx.MockTransport(handler)
    ) as client:
        status = await _status_port(client, _token(tmp_path)).read_activation_status()

    assert captured["method"] == "GET"
    assert captured["url"] == f"{STATUS_ORIGIN}/activation/status"
    assert captured["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert status.active_digest == DIGEST
    assert status.ready is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["activation", "status"])
async def test_rereads_rotated_projected_token_every_call(tmp_path: Path, kind: str):
    observed = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers["authorization"])
        payload = _accepted() if kind == "activation" else _status()
        return httpx.Response(202 if kind == "activation" else 200, json=payload)

    origin = ACTIVATION_ORIGIN if kind == "activation" else STATUS_ORIGIN
    token_path = _token(tmp_path, "first-secure-token-value-123456789")
    async with httpx.AsyncClient(
        base_url=origin, transport=httpx.MockTransport(handler)
    ) as client:
        port = (
            _activation(client, token_path)
            if kind == "activation"
            else _status_port(client, token_path)
        )
        if kind == "activation":
            await port.activate(_candidate(), operation_id=OPERATION_ID)
        else:
            await port.read_activation_status()
        token_path.write_text("rotated-secure-token-value-12345678", encoding="ascii")
        if kind == "activation":
            await port.activate(_candidate(), operation_id=OPERATION_ID)
        else:
            await port.read_activation_status()

    assert observed == [
        "Bearer first-secure-token-value-123456789",
        "Bearer rotated-secure-token-value-12345678",
    ]


@pytest.mark.asyncio
async def test_activation_replay_sends_identical_body_and_operation_id(tmp_path: Path):
    observed = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.content, request.headers["idempotency-key"]))
        return httpx.Response(202, json=_accepted())

    async with httpx.AsyncClient(
        base_url=ACTIVATION_ORIGIN, transport=httpx.MockTransport(handler)
    ) as client:
        port = _activation(client, _token(tmp_path))
        await port.activate(_candidate(), operation_id=OPERATION_ID)
        await port.activate(_candidate(), operation_id=OPERATION_ID)

    assert observed[0] == observed[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "status_code", "reason_code", "retryable"),
    [
        ("activation", 401, "AUTHORIZATION_REJECTED", False),
        ("activation", 409, "ACTIVATION_CONFLICT", False),
        ("activation", 422, "REQUEST_REJECTED", False),
        ("activation", 429, "SERVICE_UNAVAILABLE", True),
        ("activation", 503, "SERVICE_UNAVAILABLE", True),
        ("status", 403, "AUTHORIZATION_REJECTED", False),
        ("status", 404, "ENDPOINT_NOT_FOUND", False),
        ("status", 500, "SERVICE_UNAVAILABLE", True),
        ("status", 418, "REQUEST_REJECTED", False),
    ],
)
async def test_maps_http_errors_without_exposing_body_or_token(
    tmp_path: Path, kind: str, status_code: int, reason_code: str, retryable: bool
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="secret server diagnostic")

    origin = ACTIVATION_ORIGIN if kind == "activation" else STATUS_ORIGIN
    async with httpx.AsyncClient(
        base_url=origin, transport=httpx.MockTransport(handler)
    ) as client:
        port = (
            _activation(client, _token(tmp_path))
            if kind == "activation"
            else _status_port(client, _token(tmp_path))
        )
        with pytest.raises(RolloutHttpError) as raised:
            if kind == "activation":
                await port.activate(_candidate(), operation_id=OPERATION_ID)
            else:
                await port.read_activation_status()

    assert raised.value.reason_code == reason_code
    assert raised.value.retryable is retryable
    assert "secret server diagnostic" not in str(raised.value)
    assert TOKEN not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["activation", "status"])
async def test_timeout_and_network_errors_are_bounded_and_retryable(
    tmp_path: Path, kind: str
):
    origin = ACTIVATION_ORIGIN if kind == "activation" else STATUS_ORIGIN

    async def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret timeout detail", request=request)

    async with httpx.AsyncClient(
        base_url=origin, transport=httpx.MockTransport(timed_out)
    ) as client:
        port = (
            _activation(client, _token(tmp_path))
            if kind == "activation"
            else _status_port(client, _token(tmp_path))
        )
        with pytest.raises(RolloutHttpError) as raised:
            if kind == "activation":
                await port.activate(_candidate(), operation_id=OPERATION_ID)
            else:
                await port.read_activation_status()
    assert raised.value.reason_code == "SERVICE_TIMEOUT"
    assert raised.value.retryable is True
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_network_failure_is_bounded_and_retryable(tmp_path: Path):
    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret DNS detail", request=request)

    async with httpx.AsyncClient(
        base_url=STATUS_ORIGIN, transport=httpx.MockTransport(unavailable)
    ) as client:
        with pytest.raises(RolloutHttpError) as raised:
            await _status_port(client, _token(tmp_path)).read_activation_status()
    assert raised.value.reason_code == "SERVICE_UNAVAILABLE"
    assert raised.value.retryable is True
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_redirect_is_rejected_even_when_client_default_follows(tmp_path: Path):
    observed = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://attacker.invalid/steal"})

    async with httpx.AsyncClient(
        base_url=ACTIVATION_ORIGIN,
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(RolloutHttpError) as raised:
            await _activation(client, _token(tmp_path)).activate(
                _candidate(), operation_id=OPERATION_ID
            )
    assert raised.value.reason_code == "REQUEST_REJECTED"
    assert raised.value.retryable is False
    assert observed == [f"{ACTIVATION_ORIGIN}/v1/activation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["activation", "status"])
async def test_caps_success_response_before_json_validation(tmp_path: Path, kind: str):
    origin = ACTIVATION_ORIGIN if kind == "activation" else STATUS_ORIGIN

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202 if kind == "activation" else 200, content=b"x" * 1025)

    async with httpx.AsyncClient(
        base_url=origin, transport=httpx.MockTransport(handler)
    ) as client:
        port = (
            _activation(client, _token(tmp_path), maximum_response_bytes=1024)
            if kind == "activation"
            else _status_port(client, _token(tmp_path), maximum_response_bytes=1024)
        )
        with pytest.raises(RolloutHttpError) as raised:
            if kind == "activation":
                await port.activate(_candidate(), operation_id=OPERATION_ID)
            else:
                await port.read_activation_status()
    assert raised.value.reason_code == "RESPONSE_TOO_LARGE"
    assert raised.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "response"),
    [
        ("activation", httpx.Response(202, content=b"not-json", headers={"content-type": "application/json"})),
        ("activation", httpx.Response(202, json=_accepted(digest="sha256:" + "c" * 64))),
        ("activation", httpx.Response(202, json={**_accepted(), "extra": True})),
        ("activation", httpx.Response(200, json=_accepted())),
        ("status", httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"})),
        ("status", httpx.Response(200, json={**_status(), "extra": True})),
        ("status", httpx.Response(200, json=_status(ready=True, reason_code="WRONG"))),
    ],
)
async def test_rejects_invalid_wrong_target_or_wrong_status_success(
    tmp_path: Path, kind: str, response: httpx.Response
):
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    origin = ACTIVATION_ORIGIN if kind == "activation" else STATUS_ORIGIN
    async with httpx.AsyncClient(
        base_url=origin, transport=httpx.MockTransport(handler)
    ) as client:
        port = (
            _activation(client, _token(tmp_path))
            if kind == "activation"
            else _status_port(client, _token(tmp_path))
        )
        with pytest.raises(RolloutHttpError) as raised:
            if kind == "activation":
                await port.activate(_candidate(), operation_id=OPERATION_ID)
            else:
                await port.read_activation_status()
    assert raised.value.reason_code in {"INVALID_RESPONSE", "REQUEST_REJECTED"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["activation", "status"])
async def test_success_requires_json_content_type(tmp_path: Path, kind: str):
    payload = _accepted() if kind == "activation" else _status()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202 if kind == "activation" else 200,
            content=json.dumps(payload),
            headers={"content-type": "text/plain"},
        )

    origin = ACTIVATION_ORIGIN if kind == "activation" else STATUS_ORIGIN
    async with httpx.AsyncClient(
        base_url=origin, transport=httpx.MockTransport(handler)
    ) as client:
        port = (
            _activation(client, _token(tmp_path))
            if kind == "activation"
            else _status_port(client, _token(tmp_path))
        )
        with pytest.raises(RolloutHttpError) as raised:
            if kind == "activation":
                await port.activate(_candidate(), operation_id=OPERATION_ID)
            else:
                await port.read_activation_status()
    assert raised.value.reason_code == "INVALID_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "short", "x" * 513, "x" * 31 + " ", "x" * 31 + "\n", "x" * 31 + "\N{SNOWMAN}"])
async def test_invalid_credential_never_sends_request(tmp_path: Path, token: str):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202, json=_accepted())

    token_path = tmp_path / "bearer-token"
    token_path.write_text(token, encoding="utf-8")
    async with httpx.AsyncClient(
        base_url=ACTIVATION_ORIGIN, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RolloutHttpError) as raised:
            await _activation(client, token_path).activate(
                _candidate(), operation_id=OPERATION_ID
            )
    assert raised.value.reason_code == "CREDENTIAL_INVALID"
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_id",
    ["", "contains space", "x" * 129, ":starts-with-punctuation", "line\nbreak"],
)
async def test_invalid_operation_id_never_sends_activation(tmp_path: Path, operation_id: str):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202, json=_accepted())

    async with httpx.AsyncClient(
        base_url=ACTIVATION_ORIGIN, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RolloutHttpError) as raised:
            await _activation(client, _token(tmp_path)).activate(
                _candidate(), operation_id=operation_id
            )
    assert raised.value.reason_code == "INVALID_ROLLOUT_INTENT"
    assert raised.value.retryable is False
    assert calls == 0


@pytest.mark.asyncio
async def test_request_size_is_bounded_before_send(tmp_path: Path):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202, json=_accepted())

    async with httpx.AsyncClient(
        base_url=ACTIVATION_ORIGIN, transport=httpx.MockTransport(handler)
    ) as client:
        port = _activation(client, _token(tmp_path), maximum_request_bytes=1024)
        candidate = _candidate("/" + "a" * 1100)
        with pytest.raises(RolloutHttpError) as raised:
            await port.activate(candidate, operation_id=OPERATION_ID)
    assert raised.value.reason_code == "REQUEST_TOO_LARGE"
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://service:8014",
        "http://service.namespace:8014",
        "http://service.namespace.svc",
        "http://service.namespace.svc/path:8014",
        "http://user@service.namespace.svc:8014",
        "http://service.namespace.svc:8014?target=other",
        "https://attacker.invalid:8014",
        "ftp://service.namespace.svc:8014",
        "http://Service.namespace.svc:8014",
    ],
)
async def test_rejects_non_service_or_ambiguous_origins(tmp_path: Path, origin: str):
    async with httpx.AsyncClient(base_url=origin) as client:
        with pytest.raises(RolloutHttpConfigurationError):
            HttpActivationPort(
                client,
                internal_origin=origin,
                bearer_token_path=_token(tmp_path),
            )


@pytest.mark.asyncio
async def test_rejects_client_not_pinned_to_declared_origin(tmp_path: Path):
    async with httpx.AsyncClient(base_url="http://other.edge-system.svc:8014") as client:
        with pytest.raises(RolloutHttpConfigurationError, match="configured"):
            HttpActivationPort(
                client,
                internal_origin=ACTIVATION_ORIGIN,
                bearer_token_path=_token(tmp_path),
            )


@pytest.mark.asyncio
async def test_accepts_only_exact_ipv4_loopback_for_same_pod_ports(tmp_path: Path):
    origin = "http://127.0.0.1:8014"
    async with httpx.AsyncClient(base_url=origin) as client:
        port = HttpActivationPort(
            client,
            internal_origin=origin,
            bearer_token_path=_token(tmp_path),
        )
        assert port is not None

    for rejected in (
        "http://localhost:8014",
        "http://127.0.0.2:8014",
        "https://127.0.0.1:8014",
    ):
        async with httpx.AsyncClient(base_url=rejected) as client:
            with pytest.raises(RolloutHttpConfigurationError):
                HttpActivationPort(
                    client,
                    internal_origin=rejected,
                    bearer_token_path=_token(tmp_path),
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"bearer_token_path": "relative/token"},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"maximum_response_bytes": 100},
        {"maximum_response_bytes": 1024 * 1024 + 1},
        {"maximum_request_bytes": 100},
        {"maximum_request_bytes": 64 * 1024 + 1},
    ],
)
async def test_configuration_bounds_are_strict(tmp_path: Path, changes: dict):
    async with httpx.AsyncClient(base_url=ACTIVATION_ORIGIN) as client:
        with pytest.raises(RolloutHttpConfigurationError):
            _activation(client, _token(tmp_path), **changes)


@pytest.mark.asyncio
async def test_missing_token_is_retryable_and_safe(tmp_path: Path):
    async with httpx.AsyncClient(base_url=STATUS_ORIGIN) as client:
        with pytest.raises(RolloutHttpError) as raised:
            await _status_port(client, tmp_path / "missing").read_activation_status()
    assert raised.value.reason_code == "CREDENTIAL_UNAVAILABLE"
    assert raised.value.retryable is True
    assert str(tmp_path) not in str(raised.value)
