"""Narrow in-cluster Kubernetes adapter for restarting the RAG workload.

The adapter deliberately implements only one operation against one Deployment:
patching a fixed pod-template annotation.  It uses the Kubernetes HTTP API
directly so the field runtime does not need the Kubernetes Python client.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import httpx


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DNS_SUBDOMAIN = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
)
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_OPERATION_ANNOTATION = "lilevy.edge/restart-operation-id"
_TARGET_DIGEST_ANNOTATION = "lilevy.edge/target-digest"
_TARGET_SEQUENCE_ANNOTATION = "lilevy.edge/target-sequence"
_MAX_TOKEN_BYTES = 16 * 1024
_IN_CLUSTER_API_ORIGIN = "https://kubernetes.default.svc"


class DeploymentRestartConfigurationError(ValueError):
    """The fixed restart target or local adapter configuration is invalid."""


class DeploymentRestartError(RuntimeError):
    """A bounded restart failure that never contains response or credential data."""

    def __init__(self, reason_code: str, message: str, *, retryable: bool) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class DeploymentRestartResult:
    """Proof that Kubernetes accepted a patch for the configured Deployment."""

    namespace: str
    deployment_name: str
    operation_id: str
    target_digest: str
    target_sequence: int
    generation: int
    resource_version: str


class KubernetesDeploymentRestarter:
    """Patch one in-cluster Deployment's pod-template restart annotation."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        namespace: str,
        deployment_name: str,
        service_account_token_path: Path | str,
        timeout_seconds: float = 10.0,
        maximum_response_bytes: int = 64 * 1024,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise DeploymentRestartConfigurationError(
                "client must be an httpx AsyncClient"
            )
        if str(client.base_url).rstrip("/") != _IN_CLUSTER_API_ORIGIN:
            raise DeploymentRestartConfigurationError(
                "client must target the in-cluster Kubernetes API"
            )
        if not _DNS_LABEL.fullmatch(namespace):
            raise DeploymentRestartConfigurationError("namespace is invalid")
        if (
            len(deployment_name) > 253
            or not _DNS_SUBDOMAIN.fullmatch(deployment_name)
            or any(not _DNS_LABEL.fullmatch(label) for label in deployment_name.split("."))
        ):
            raise DeploymentRestartConfigurationError("deployment name is invalid")
        token_path = Path(service_account_token_path)
        if not token_path.is_absolute():
            raise DeploymentRestartConfigurationError(
                "service-account token path must be absolute"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0.1 <= timeout_seconds <= 60.0
        ):
            raise DeploymentRestartConfigurationError(
                "timeout must be between 0.1 and 60 seconds"
            )
        if (
            type(maximum_response_bytes) is not int
            or not 1024 <= maximum_response_bytes <= 1024 * 1024
        ):
            raise DeploymentRestartConfigurationError(
                "maximum response bytes must be between 1024 and 1048576"
            )
        self._client = client
        self._namespace = namespace
        self._deployment_name = deployment_name
        self._token_path = token_path
        self._timeout = float(timeout_seconds)
        self._maximum_response_bytes = maximum_response_bytes
        self._path = (
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"
        )

    async def request_restart(
        self,
        *,
        operation_id: str,
        target_digest: str,
        target_sequence: int,
    ) -> DeploymentRestartResult:
        """Idempotently request a restart for one persisted rollout intent."""

        self._validate_rollout_intent(operation_id, target_digest, target_sequence)
        credential = self._read_service_account_token()
        body = json.dumps(
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                _OPERATION_ANNOTATION: operation_id,
                                _TARGET_DIGEST_ANNOTATION: target_digest,
                                _TARGET_SEQUENCE_ANNOTATION: str(target_sequence),
                            }
                        }
                    }
                }
            },
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {credential}",
            "content-type": "application/strategic-merge-patch+json",
        }

        try:
            async with self._client.stream(
                "PATCH",
                self._path,
                headers=headers,
                content=body,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                response_body = await self._bounded_body(response)
                status_code = response.status_code
        except httpx.TimeoutException as exc:
            raise DeploymentRestartError(
                "API_TIMEOUT", "The workload restart request timed out.", retryable=True
            ) from exc
        except httpx.RequestError as exc:
            raise DeploymentRestartError(
                "API_UNAVAILABLE",
                "The workload restart API is unavailable.",
                retryable=True,
            ) from exc

        if status_code != 200:
            reason, message, retryable = self._http_failure(status_code)
            raise DeploymentRestartError(reason, message, retryable=retryable)
        return self._validated_result(
            response_body, operation_id, target_digest, target_sequence
        )

    @staticmethod
    def _validate_rollout_intent(
        operation_id: str, target_digest: str, target_sequence: int
    ) -> None:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(
            operation_id
        ):
            raise DeploymentRestartError(
                "INVALID_ROLLOUT_INTENT",
                "The workload restart intent is invalid.",
                retryable=False,
            )
        if not isinstance(target_digest, str) or not _DIGEST.fullmatch(target_digest):
            raise DeploymentRestartError(
                "INVALID_ROLLOUT_INTENT",
                "The workload restart intent is invalid.",
                retryable=False,
            )
        if (
            type(target_sequence) is not int
            or target_sequence < 1
            or target_sequence > 2**63 - 1
        ):
            raise DeploymentRestartError(
                "INVALID_ROLLOUT_INTENT",
                "The workload restart intent is invalid.",
                retryable=False,
            )

    def _read_service_account_token(self) -> str:
        try:
            token_bytes = self._token_path.read_bytes()
        except OSError as exc:
            raise DeploymentRestartError(
                "CREDENTIAL_UNAVAILABLE",
                "The service-account credential is unavailable.",
                retryable=True,
            ) from exc
        if not 1 <= len(token_bytes) <= _MAX_TOKEN_BYTES:
            raise DeploymentRestartError(
                "CREDENTIAL_INVALID",
                "The service-account credential is invalid.",
                retryable=False,
            )
        try:
            token = token_bytes.decode("ascii")
        except UnicodeDecodeError as exc:
            raise DeploymentRestartError(
                "CREDENTIAL_INVALID",
                "The service-account credential is invalid.",
                retryable=False,
            ) from exc
        if any(character.isspace() or not character.isprintable() for character in token):
            raise DeploymentRestartError(
                "CREDENTIAL_INVALID",
                "The service-account credential is invalid.",
                retryable=False,
            )
        return token

    async def _bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = -1
            if declared_size > self._maximum_response_bytes:
                raise DeploymentRestartError(
                    "RESPONSE_TOO_LARGE",
                    "The workload restart response was too large.",
                    retryable=False,
                )

        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._maximum_response_bytes:
                raise DeploymentRestartError(
                    "RESPONSE_TOO_LARGE",
                    "The workload restart response was too large.",
                    retryable=False,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _validated_result(
        self,
        response_body: bytes,
        operation_id: str,
        target_digest: str,
        target_sequence: int,
    ) -> DeploymentRestartResult:
        try:
            payload = json.loads(response_body)
            metadata = payload["metadata"]
            name = metadata["name"]
            namespace = metadata["namespace"]
            generation = metadata["generation"]
            resource_version = metadata["resourceVersion"]
            annotations = payload["spec"]["template"]["metadata"]["annotations"]
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
            raise DeploymentRestartError(
                "INVALID_RESPONSE",
                "The workload restart response was invalid.",
                retryable=False,
            ) from exc
        if (
            name != self._deployment_name
            or namespace != self._namespace
            or type(generation) is not int
            or generation < 1
            or not isinstance(resource_version, str)
            or not 1 <= len(resource_version) <= 128
            or any(
                character.isspace() or not character.isprintable()
                for character in resource_version
            )
            or not isinstance(annotations, dict)
            or annotations.get(_OPERATION_ANNOTATION) != operation_id
            or annotations.get(_TARGET_DIGEST_ANNOTATION) != target_digest
            or annotations.get(_TARGET_SEQUENCE_ANNOTATION) != str(target_sequence)
        ):
            raise DeploymentRestartError(
                "INVALID_RESPONSE",
                "The workload restart response was invalid.",
                retryable=False,
            )
        return DeploymentRestartResult(
            namespace=self._namespace,
            deployment_name=self._deployment_name,
            operation_id=operation_id,
            target_digest=target_digest,
            target_sequence=target_sequence,
            generation=generation,
            resource_version=resource_version,
        )

    @staticmethod
    def _http_failure(status_code: int) -> tuple[str, str, bool]:
        if status_code in {401, 403}:
            return (
                "AUTHORIZATION_REJECTED",
                "The workload restart was not authorized.",
                False,
            )
        if status_code == 404:
            return "DEPLOYMENT_NOT_FOUND", "The restart target was not found.", False
        if status_code == 409:
            return "API_CONFLICT", "The workload restart conflicted.", True
        if status_code == 429 or 500 <= status_code <= 599:
            return "API_UNAVAILABLE", "The workload restart API is unavailable.", True
        return "API_REJECTED", "The workload restart request was rejected.", False
