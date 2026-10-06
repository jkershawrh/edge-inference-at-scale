"""OpenShift runtime composition for the autonomous corpus rollout controller."""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI

from .activation_contracts import ActivationCandidateRequest
from .deployment_restart import KubernetesDeploymentRestarter
from .deployment_rollout import (
    DeploymentRolloutController,
    LocalRolloutStateStore,
    RolloutConfiguration,
    RolloutPhase,
    RolloutPortError,
)
from .deployment_rollout_api import create_deployment_rollout_app
from .deployment_rollout_http import HttpActivationPort, HttpStatusPort


_KUBERNETES_ORIGIN = "https://kubernetes.default.svc"
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DNS_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_REQUIRED_ENVIRONMENT = (
    "ROLLOUT_API_TOKEN_PATH",
    "ACTIVATION_BEARER_TOKEN_PATH",
    "SERVICE_ACCOUNT_TOKEN_PATH",
    "SERVICE_ACCOUNT_NAMESPACE_PATH",
    "SERVICE_ACCOUNT_CA_PATH",
    "ACTIVATION_INTERNAL_ORIGIN",
    "RAG_INTERNAL_ORIGIN",
    "ROLLOUT_CANDIDATE_PATH",
    "ROLLOUT_CANDIDATE_DIGEST",
    "ROLLOUT_CANDIDATE_SEQUENCE",
    "ROLLOUT_STATE_PATH",
    "RAG_DEPLOYMENT_NAME",
    "ROLLOUT_MAX_ACTIVATION_ATTEMPTS",
    "ROLLOUT_MAX_RESTART_ATTEMPTS",
    "ROLLOUT_MAX_STATUS_POLLS",
    "ROLLOUT_TIMEOUT_SECONDS",
    "ROLLOUT_DEPENDENCY_TIMEOUT_SECONDS",
    "ROLLOUT_WORKER_POLL_SECONDS",
)


class RolloutRuntimeConfigurationError(ValueError):
    """The sidecar environment or one of its mounted files is invalid."""


class RolloutRuntimeSettings:
    """Validated, immutable values needed to construct the rollout runtime."""

    __slots__ = (
        "rollout_api_token_path",
        "activation_token_path",
        "service_account_token_path",
        "service_account_namespace_path",
        "service_account_ca_path",
        "activation_origin",
        "rag_origin",
        "candidate",
        "state_path",
        "deployment_name",
        "namespace",
        "configuration",
        "dependency_timeout_seconds",
        "worker_poll_seconds",
        "rollout_api_credential",
    )

    def __init__(self, source: Mapping[str, str]) -> None:
        try:
            if any(
                not isinstance(source[name], str) or not source[name]
                for name in _REQUIRED_ENVIRONMENT
            ):
                raise ValueError
            self.rollout_api_token_path = _projected_file(
                source["ROLLOUT_API_TOKEN_PATH"]
            )
            self.activation_token_path = _projected_file(
                source["ACTIVATION_BEARER_TOKEN_PATH"]
            )
            self.service_account_token_path = _projected_file(
                source["SERVICE_ACCOUNT_TOKEN_PATH"]
            )
            self.service_account_namespace_path = _projected_file(
                source["SERVICE_ACCOUNT_NAMESPACE_PATH"]
            )
            self.service_account_ca_path = _projected_file(
                source["SERVICE_ACCOUNT_CA_PATH"]
            )
            self.rollout_api_credential = _ascii_credential(
                self.rollout_api_token_path, minimum=32, maximum=512
            )
            activation_credential = _ascii_credential(
                self.activation_token_path, minimum=32, maximum=512
            )
            service_account_credential = _ascii_credential(
                self.service_account_token_path, minimum=1, maximum=16 * 1024
            )
            credential_paths = {
                path.resolve(strict=True)
                for path in (
                    self.rollout_api_token_path,
                    self.activation_token_path,
                    self.service_account_token_path,
                )
            }
            if len(credential_paths) != 3 or len(
                {
                    self.rollout_api_credential,
                    activation_credential,
                    service_account_credential,
                }
            ) != 3:
                raise ValueError
            raw_namespace = self.service_account_namespace_path.read_text(encoding="ascii")
            namespace = raw_namespace[:-1] if raw_namespace.endswith("\n") else raw_namespace
            if "\n" in namespace or "\r" in namespace:
                raise ValueError
            if not _DNS_LABEL.fullmatch(namespace):
                raise ValueError
            if self.service_account_ca_path.stat().st_size not in range(1, 1024 * 1024 + 1):
                raise ValueError
            self.namespace = namespace
            self.activation_origin = _internal_origin(
                source["ACTIVATION_INTERNAL_ORIGIN"]
            )
            self.rag_origin = _internal_origin(source["RAG_INTERNAL_ORIGIN"])
            self.candidate = ActivationCandidateRequest(
                package_path=source["ROLLOUT_CANDIDATE_PATH"],
                digest=source["ROLLOUT_CANDIDATE_DIGEST"],
                sequence=_integer(source["ROLLOUT_CANDIDATE_SEQUENCE"], 1, 2**63 - 1),
            )
            self.state_path = Path(source["ROLLOUT_STATE_PATH"])
            if not self.state_path.is_absolute():
                raise ValueError
            self.deployment_name = source["RAG_DEPLOYMENT_NAME"]
            if (
                len(self.deployment_name) > 253
                or not _DNS_SUBDOMAIN.fullmatch(self.deployment_name)
                or any(
                    not _DNS_LABEL.fullmatch(label)
                    for label in self.deployment_name.split(".")
                )
            ):
                raise ValueError
            self.configuration = RolloutConfiguration(
                maximum_activation_attempts=_integer(
                    source["ROLLOUT_MAX_ACTIVATION_ATTEMPTS"], 1, 100
                ),
                maximum_restart_attempts=_integer(
                    source["ROLLOUT_MAX_RESTART_ATTEMPTS"], 1, 100
                ),
                maximum_status_polls=_integer(
                    source["ROLLOUT_MAX_STATUS_POLLS"], 1, 10000
                ),
                timeout_seconds=_integer(source["ROLLOUT_TIMEOUT_SECONDS"], 1, 86400),
            )
            self.dependency_timeout_seconds = _number(
                source["ROLLOUT_DEPENDENCY_TIMEOUT_SECONDS"], 0.1, 60.0
            )
            self.worker_poll_seconds = _number(
                source["ROLLOUT_WORKER_POLL_SECONDS"], 1.0, 60.0
            )
        except (KeyError, OSError, UnicodeError, TypeError, ValueError):
            raise RolloutRuntimeConfigurationError(
                "rollout runtime environment configuration is invalid"
            ) from None


def _integer(value: str, minimum: int, maximum: int) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ValueError
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError
    return parsed


def _number(value: str, minimum: float, maximum: float) -> float:
    parsed = float(value)
    if not minimum <= parsed <= maximum:
        raise ValueError
    return parsed


def _internal_origin(value: str) -> str:
    if value.endswith("/"):
        raise ValueError
    parsed = urlsplit(value)
    labels = (parsed.hostname or "").split(".")
    loopback = parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    service_name = len(labels) == 3 and labels[2] == "svc"
    cluster_name = len(labels) == 5 and labels[2:] == ["svc", "cluster", "local"]
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
        or not (loopback or service_name or cluster_name)
        or (not loopback and not all(_DNS_LABEL.fullmatch(label) for label in labels))
    ):
        raise ValueError
    return value


def _projected_file(value: str) -> Path:
    """Accept a regular file or a projected-volume link contained by its mount."""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError
    for directory in (path.parent, *path.parent.parents):
        if directory.is_symlink():
            raise ValueError
    parent = path.parent.resolve(strict=True)
    resolved = path.resolve(strict=True)
    resolved.relative_to(parent)
    if not resolved.is_file():
        raise ValueError
    return path


def _ascii_credential(path: Path, *, minimum: int, maximum: int) -> str:
    raw = path.read_bytes()
    if not minimum <= len(raw) <= maximum:
        raise ValueError
    value = raw.decode("ascii")
    if any(character.isspace() or not character.isprintable() for character in value):
        raise ValueError
    return value


ClientFactory = Callable[..., httpx.AsyncClient]
Sleep = Callable[[float], Awaitable[None]]


def _http_client(*, base_url: str, verify: bool | str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        verify=verify,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )


def create_deployment_rollout_from_environment(
    environment: Optional[Mapping[str, str]] = None,
    *,
    client_factory: ClientFactory = _http_client,
    sleep: Sleep = asyncio.sleep,
) -> FastAPI:
    """No-argument-compatible ASGI factory for the OpenShift rollout sidecar."""
    settings = RolloutRuntimeSettings(os.environ if environment is None else environment)
    controller_holder: dict[str, DeploymentRolloutController] = {}
    health = {"status": "starting"}
    operation_lock = asyncio.Lock()

    def controller() -> DeploymentRolloutController:
        return controller_holder["controller"]

    async def worker() -> None:
        while True:
            try:
                async with operation_lock:
                    result = await controller().advance()
            except RolloutPortError:
                health["status"] = "running"
            except asyncio.CancelledError:
                raise
            except Exception:
                health["status"] = "terminal_failure"
                return
            else:
                if result.complete:
                    health["status"] = (
                        "complete"
                        if result.phase is RolloutPhase.VERIFIED
                        else "terminal_failure"
                    )
                    return
                health["status"] = "running"
            await sleep(settings.worker_poll_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        clients: list[httpx.AsyncClient] = []
        task: Optional[asyncio.Task[None]] = None
        try:
            try:
                activation_client = client_factory(
                    base_url=settings.activation_origin, verify=True
                )
                if not isinstance(activation_client, httpx.AsyncClient):
                    raise TypeError
                clients.append(activation_client)
                status_client = client_factory(
                    base_url=settings.rag_origin, verify=True
                )
                if not isinstance(status_client, httpx.AsyncClient):
                    raise TypeError
                clients.append(status_client)
                kubernetes_client = client_factory(
                    base_url=_KUBERNETES_ORIGIN,
                    verify=str(settings.service_account_ca_path.resolve(strict=True)),
                )
                if not isinstance(kubernetes_client, httpx.AsyncClient):
                    raise TypeError
                clients.append(kubernetes_client)
                rollout = DeploymentRolloutController(
                    settings.candidate,
                    activation=HttpActivationPort(
                        activation_client,
                        internal_origin=settings.activation_origin,
                        bearer_token_path=settings.activation_token_path,
                        timeout_seconds=settings.dependency_timeout_seconds,
                    ),
                    workload=KubernetesDeploymentRestarter(
                        kubernetes_client,
                        namespace=settings.namespace,
                        deployment_name=settings.deployment_name,
                        service_account_token_path=settings.service_account_token_path,
                        timeout_seconds=settings.dependency_timeout_seconds,
                    ),
                    status=HttpStatusPort(
                        status_client,
                        internal_origin=settings.rag_origin,
                        bearer_token_path=settings.activation_token_path,
                        timeout_seconds=settings.dependency_timeout_seconds,
                    ),
                    store=LocalRolloutStateStore(settings.state_path),
                    configuration=settings.configuration,
                )
                controller_holder["controller"] = rollout
                async with operation_lock:
                    initial = rollout.current_result()
                if initial.complete:
                    health["status"] = (
                        "complete"
                        if initial.phase is RolloutPhase.VERIFIED
                        else "terminal_failure"
                    )
                else:
                    health["status"] = "running"
                    task = asyncio.create_task(worker(), name="lil-evy-rollout-worker")
            except Exception:
                health["status"] = "terminal_failure"
                raise RolloutRuntimeConfigurationError(
                    "rollout runtime could not be initialized"
                ) from None
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            controller_holder.clear()
            for client in reversed(clients):
                await client.aclose()

    return create_deployment_rollout_app(
        controller,
        bearer_credential=settings.rollout_api_credential,
        operation_lock=operation_lock,
        lifespan=lifespan,
        health_status=lambda: health["status"],
    )
