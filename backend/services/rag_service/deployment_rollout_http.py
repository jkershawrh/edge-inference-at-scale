"""Bounded in-cluster HTTP ports for corpus rollout activation and status.

The adapters accept only an injected client pinned to an explicit Kubernetes
Service origin or exact same-pod IPv4 loopback origin. They never follow
redirects, reread their projected bearer credential for every call, and expose
only stable error codes to the rollout controller.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .activation_contracts import (
    ActivationAcceptedResponse,
    ActivationCandidateRequest,
    ActivationStatusResponse,
)


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_TOKEN_BYTES = 512
_MIN_TOKEN_BYTES = 32


class RolloutHttpConfigurationError(ValueError):
    """An HTTP rollout adapter is not safely scoped or bounded."""


class RolloutHttpError(RuntimeError):
    """A bounded HTTP port failure with an explicit retry classification."""

    def __init__(self, reason_code: str, message: str, *, retryable: bool) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        super().__init__(message)


class _InClusterHttpPort:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        internal_origin: str,
        bearer_token_path: Path | str,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise RolloutHttpConfigurationError("client must be an httpx AsyncClient")
        normalized_origin = self._validated_origin(internal_origin)
        if str(client.base_url).rstrip("/") != normalized_origin:
            raise RolloutHttpConfigurationError(
                "client must target the configured internal Service origin"
            )
        token_path = Path(bearer_token_path)
        if not token_path.is_absolute():
            raise RolloutHttpConfigurationError("bearer-token path must be absolute")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0.1 <= timeout_seconds <= 60.0
        ):
            raise RolloutHttpConfigurationError(
                "timeout must be between 0.1 and 60 seconds"
            )
        if (
            type(maximum_response_bytes) is not int
            or not 1024 <= maximum_response_bytes <= 1024 * 1024
        ):
            raise RolloutHttpConfigurationError(
                "maximum response bytes must be between 1024 and 1048576"
            )
        self._client = client
        self._token_path = token_path
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._maximum_response_bytes = maximum_response_bytes

    @staticmethod
    def _validated_origin(value: str) -> str:
        if not isinstance(value, str) or not value or value.endswith("/"):
            raise RolloutHttpConfigurationError(
                "internal origin must be an absolute Kubernetes Service origin"
            )
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise RolloutHttpConfigurationError(
                "internal origin must be an absolute Kubernetes Service origin"
            ) from exc
        hostname = parsed.hostname or ""
        labels = hostname.split(".")
        loopback = parsed.scheme == "http" and hostname == "127.0.0.1"
        service_suffix = len(labels) == 3 and labels[2] == "svc"
        cluster_suffix = len(labels) == 5 and labels[2:] == ["svc", "cluster", "local"]
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != ""
            or parsed.query
            or parsed.fragment
            or port is None
            or not 1 <= port <= 65535
            or not (loopback or service_suffix or cluster_suffix)
            or (not loopback and not all(_DNS_LABEL.fullmatch(label) for label in labels))
        ):
            raise RolloutHttpConfigurationError(
                "internal origin must be an absolute Kubernetes Service origin"
            )
        return value

    def _read_bearer_token(self) -> str:
        try:
            token_bytes = self._token_path.read_bytes()
        except OSError as exc:
            raise RolloutHttpError(
                "CREDENTIAL_UNAVAILABLE",
                "The rollout HTTP credential is unavailable.",
                retryable=True,
            ) from exc
        if not _MIN_TOKEN_BYTES <= len(token_bytes) <= _MAX_TOKEN_BYTES:
            raise RolloutHttpError(
                "CREDENTIAL_INVALID",
                "The rollout HTTP credential is invalid.",
                retryable=False,
            )
        try:
            token = token_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RolloutHttpError(
                "CREDENTIAL_INVALID",
                "The rollout HTTP credential is invalid.",
                retryable=False,
            ) from exc
        if any(character.isspace() or not character.isprintable() for character in token):
            raise RolloutHttpError(
                "CREDENTIAL_INVALID",
                "The rollout HTTP credential is invalid.",
                retryable=False,
            )
        return token

    async def _bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise RolloutHttpError(
                    "INVALID_RESPONSE",
                    "The rollout HTTP response was invalid.",
                    retryable=False,
                ) from exc
            if declared_size < 0:
                raise RolloutHttpError(
                    "INVALID_RESPONSE",
                    "The rollout HTTP response was invalid.",
                    retryable=False,
                )
            if declared_size > self._maximum_response_bytes:
                raise RolloutHttpError(
                    "RESPONSE_TOO_LARGE",
                    "The rollout HTTP response was too large.",
                    retryable=False,
                )
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._maximum_response_bytes:
                raise RolloutHttpError(
                    "RESPONSE_TOO_LARGE",
                    "The rollout HTTP response was too large.",
                    retryable=False,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _is_json(response: httpx.Response) -> bool:
        return (
            response.headers.get("content-type", "")
            .partition(";")[0]
            .strip()
            .lower()
            == "application/json"
        )

    @staticmethod
    def _http_failure(status_code: int, operation: str) -> RolloutHttpError:
        if status_code in {401, 403}:
            return RolloutHttpError(
                "AUTHORIZATION_REJECTED",
                f"The rollout {operation} was not authorized.",
                retryable=False,
            )
        if status_code == 404:
            return RolloutHttpError(
                "ENDPOINT_NOT_FOUND",
                f"The rollout {operation} endpoint was not found.",
                retryable=False,
            )
        if status_code == 409 and operation == "activation":
            return RolloutHttpError(
                "ACTIVATION_CONFLICT",
                "The corpus activation request conflicted.",
                retryable=False,
            )
        if status_code == 429 or 500 <= status_code <= 599:
            return RolloutHttpError(
                "SERVICE_UNAVAILABLE",
                f"The rollout {operation} service is unavailable.",
                retryable=True,
            )
        return RolloutHttpError(
            "REQUEST_REJECTED",
            f"The rollout {operation} request was rejected.",
            retryable=False,
        )


class HttpActivationPort(_InClusterHttpPort):
    """ActivationPort bound to one internal or same-pod operator origin."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        internal_origin: str,
        bearer_token_path: Path | str,
        timeout_seconds: float = 10.0,
        maximum_request_bytes: int = 8 * 1024,
        maximum_response_bytes: int = 64 * 1024,
    ) -> None:
        super().__init__(
            client,
            internal_origin=internal_origin,
            bearer_token_path=bearer_token_path,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )
        if (
            type(maximum_request_bytes) is not int
            or not 1024 <= maximum_request_bytes <= 64 * 1024
        ):
            raise RolloutHttpConfigurationError(
                "maximum request bytes must be between 1024 and 65536"
            )
        self._maximum_request_bytes = maximum_request_bytes

    async def activate(
        self, candidate: ActivationCandidateRequest, *, operation_id: str
    ) -> ActivationAcceptedResponse:
        if not isinstance(candidate, ActivationCandidateRequest):
            raise RolloutHttpError(
                "INVALID_ROLLOUT_INTENT",
                "The corpus activation intent is invalid.",
                retryable=False,
            )
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise RolloutHttpError(
                "INVALID_ROLLOUT_INTENT",
                "The corpus activation intent is invalid.",
                retryable=False,
            )
        body = candidate.model_dump_json().encode("utf-8")
        if len(body) > self._maximum_request_bytes:
            raise RolloutHttpError(
                "REQUEST_TOO_LARGE",
                "The corpus activation request was too large.",
                retryable=False,
            )
        token = self._read_bearer_token()
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "idempotency-key": operation_id,
        }
        try:
            async with self._client.stream(
                "POST",
                "/v1/activation",
                headers=headers,
                content=body,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                response_body = await self._bounded_body(response)
                status_code = response.status_code
                is_json = self._is_json(response)
        except RolloutHttpError:
            raise
        except httpx.TimeoutException as exc:
            raise RolloutHttpError(
                "SERVICE_TIMEOUT",
                "The corpus activation request timed out.",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise RolloutHttpError(
                "SERVICE_UNAVAILABLE",
                "The corpus activation service is unavailable.",
                retryable=True,
            ) from exc
        if status_code != 202:
            raise self._http_failure(status_code, "activation")
        if not is_json:
            raise RolloutHttpError(
                "INVALID_RESPONSE",
                "The corpus activation response was invalid.",
                retryable=False,
            )
        try:
            payload: Any = json.loads(response_body)
            accepted = ActivationAcceptedResponse.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
            raise RolloutHttpError(
                "INVALID_RESPONSE",
                "The corpus activation response was invalid.",
                retryable=False,
            ) from exc
        if (
            accepted.receipt.desired.digest != candidate.digest
            or accepted.receipt.desired.sequence != candidate.sequence
        ):
            raise RolloutHttpError(
                "INVALID_RESPONSE",
                "The corpus activation response was invalid.",
                retryable=False,
            )
        return accepted


class HttpStatusPort(_InClusterHttpPort):
    """StatusPort bound to one internal or same-pod RAG origin."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        internal_origin: str,
        bearer_token_path: Path | str,
        timeout_seconds: float = 10.0,
        maximum_response_bytes: int = 64 * 1024,
    ) -> None:
        super().__init__(
            client,
            internal_origin=internal_origin,
            bearer_token_path=bearer_token_path,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )

    async def read_activation_status(self) -> ActivationStatusResponse:
        token = self._read_bearer_token()
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {token}",
        }
        try:
            async with self._client.stream(
                "GET",
                "/activation/status",
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                response_body = await self._bounded_body(response)
                status_code = response.status_code
                is_json = self._is_json(response)
        except RolloutHttpError:
            raise
        except httpx.TimeoutException as exc:
            raise RolloutHttpError(
                "SERVICE_TIMEOUT",
                "The activation status request timed out.",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise RolloutHttpError(
                "SERVICE_UNAVAILABLE",
                "The activation status service is unavailable.",
                retryable=True,
            ) from exc
        if status_code != 200:
            raise self._http_failure(status_code, "status")
        if not is_json:
            raise RolloutHttpError(
                "INVALID_RESPONSE",
                "The activation status response was invalid.",
                retryable=False,
            )
        try:
            payload: Any = json.loads(response_body)
            return ActivationStatusResponse.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError) as exc:
            raise RolloutHttpError(
                "INVALID_RESPONSE",
                "The activation status response was invalid.",
                retryable=False,
            ) from exc
