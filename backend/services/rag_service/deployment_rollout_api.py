"""Internal HTTP boundary for the transport-neutral deployment rollout core."""
from __future__ import annotations

import asyncio
import hmac
from enum import Enum
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Callable, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .activation_contracts import StrictContract
from .deployment_rollout import (
    DeploymentRolloutController,
    RolloutPortError,
    RolloutResult,
    RolloutStateError,
)


MINIMUM_CREDENTIAL_BYTES = 32
MAXIMUM_CREDENTIAL_BYTES = 512
MAXIMUM_AUTHORIZATION_HEADER_BYTES = len("Bearer ") + MAXIMUM_CREDENTIAL_BYTES
MAXIMUM_REQUEST_BYTES = 256


class RolloutApiError(str, Enum):
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    INVALID_REQUEST = "invalid_request"
    REQUEST_TOO_LARGE = "request_too_large"
    STATE_UNAVAILABLE = "state_unavailable"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    INTERNAL_ERROR = "internal_error"


class RolloutApiErrorResponse(StrictContract):
    error: RolloutApiError
    reason_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]
    message: str = Field(min_length=1, max_length=160)


class RolloutApiHealthResponse(StrictContract):
    service: Literal["lil-evy-deployment-rollout"]
    status: Literal["ready", "starting", "running", "complete", "terminal_failure"]
    version: Literal["1"]


class _MalformedAuthorization(ValueError):
    pass


class _RequestTooLarge(ValueError):
    pass


def _validated_credential(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("bearer_credential must be a string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("bearer_credential must contain printable ASCII") from exc
    if not MINIMUM_CREDENTIAL_BYTES <= len(encoded) <= MAXIMUM_CREDENTIAL_BYTES:
        raise ValueError("bearer_credential must be between 32 and 512 bytes")
    if any(byte < 0x21 or byte > 0x7E for byte in encoded):
        raise ValueError("bearer_credential must contain printable ASCII without whitespace")
    return encoded


def _presented_credential(request: Request) -> bytes:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise _MalformedAuthorization
    header = values[0]
    try:
        encoded = header.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _MalformedAuthorization from exc
    if len(encoded) > MAXIMUM_AUTHORIZATION_HEADER_BYTES:
        raise _MalformedAuthorization
    prefix = b"Bearer "
    if not encoded.startswith(prefix):
        raise _MalformedAuthorization
    token = encoded[len(prefix):]
    if not token or any(byte < 0x21 or byte > 0x7E for byte in token):
        raise _MalformedAuthorization
    return token


def _error(
    status_code: int,
    error: RolloutApiError,
    reason_code: str,
    message: str,
    *,
    authenticate: bool = False,
) -> JSONResponse:
    response = RolloutApiErrorResponse(
        error=error,
        reason_code=reason_code,
        message=message,
    )
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json"),
        headers={"WWW-Authenticate": "Bearer"} if authenticate else None,
    )


def _authenticate(request: Request, expected: bytes) -> JSONResponse | None:
    try:
        presented = _presented_credential(request)
    except _MalformedAuthorization:
        return _error(
            401,
            RolloutApiError.UNAUTHORIZED,
            "INVALID_AUTHORIZATION",
            "A single valid Bearer authorization header is required.",
            authenticate=True,
        )
    if not hmac.compare_digest(presented, expected):
        return _error(
            403,
            RolloutApiError.FORBIDDEN,
            "CREDENTIAL_REJECTED",
            "The rollout credential was not accepted.",
        )
    return None


async def _require_empty_body(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise ValueError("invalid request length") from exc
        if declared < 0:
            raise ValueError("invalid request length")
        if declared > MAXIMUM_REQUEST_BYTES:
            raise _RequestTooLarge

    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAXIMUM_REQUEST_BYTES:
            raise _RequestTooLarge
    if size:
        raise ValueError("request body must be empty")


def create_deployment_rollout_app(
    controller: DeploymentRolloutController | Callable[[], DeploymentRolloutController],
    *,
    bearer_credential: str,
    operation_lock: Optional[asyncio.Lock] = None,
    lifespan: Optional[
        Callable[[FastAPI], AbstractAsyncContextManager[None]]
    ] = None,
    health_status: Optional[
        Callable[[], Literal["starting", "running", "complete", "terminal_failure"]]
    ] = None,
) -> FastAPI:
    """Create an internal-only, authenticated API around one rollout."""

    if not isinstance(controller, DeploymentRolloutController) and not callable(controller):
        raise ValueError("controller must be a DeploymentRolloutController")
    expected_credential = _validated_credential(bearer_credential)
    operation_lock = operation_lock or asyncio.Lock()

    def configured_controller() -> DeploymentRolloutController:
        value = controller() if callable(controller) else controller
        if not isinstance(value, DeploymentRolloutController):
            raise RuntimeError("deployment rollout controller is unavailable")
        return value

    app = FastAPI(
        title="Lil EVY Deployment Rollout",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health", response_model=RolloutApiHealthResponse)
    async def health() -> RolloutApiHealthResponse:
        return RolloutApiHealthResponse(
            service="lil-evy-deployment-rollout",
            status=health_status() if health_status is not None else "ready",
            version="1",
        )

    @app.get("/v1/rollout", response_model=RolloutResult)
    async def current(request: Request) -> RolloutResult | JSONResponse:
        rejection = _authenticate(request, expected_credential)
        if rejection is not None:
            return rejection
        try:
            await _require_empty_body(request)
        except _RequestTooLarge:
            return _error(
                413,
                RolloutApiError.REQUEST_TOO_LARGE,
                "REQUEST_TOO_LARGE",
                "The rollout request exceeds the configured size limit.",
            )
        except ValueError:
            return _error(
                422,
                RolloutApiError.INVALID_REQUEST,
                "INVALID_REQUEST",
                "The rollout status request must have an empty body.",
            )
        try:
            async with operation_lock:
                return configured_controller().current_result()
        except RolloutStateError:
            return _error(
                409,
                RolloutApiError.STATE_UNAVAILABLE,
                "ROLLOUT_STATE_UNAVAILABLE",
                "The persisted rollout state is unavailable.",
            )
        except Exception:
            return _error(
                500,
                RolloutApiError.INTERNAL_ERROR,
                "ROLLOUT_INTERNAL_ERROR",
                "The rollout status could not be read.",
            )

    @app.post("/v1/rollout/advance", response_model=RolloutResult)
    async def advance(request: Request) -> RolloutResult | JSONResponse:
        rejection = _authenticate(request, expected_credential)
        if rejection is not None:
            return rejection
        try:
            await _require_empty_body(request)
        except _RequestTooLarge:
            return _error(
                413,
                RolloutApiError.REQUEST_TOO_LARGE,
                "REQUEST_TOO_LARGE",
                "The rollout request exceeds the configured size limit.",
            )
        except ValueError:
            return _error(
                422,
                RolloutApiError.INVALID_REQUEST,
                "INVALID_REQUEST",
                "The rollout advance request must have an empty body.",
            )

        try:
            async with operation_lock:
                return await configured_controller().advance()
        except RolloutPortError:
            return _error(
                503,
                RolloutApiError.DEPENDENCY_UNAVAILABLE,
                "ROLLOUT_DEPENDENCY_UNAVAILABLE",
                "A rollout dependency is temporarily unavailable.",
            )
        except RolloutStateError:
            return _error(
                409,
                RolloutApiError.STATE_UNAVAILABLE,
                "ROLLOUT_STATE_UNAVAILABLE",
                "The persisted rollout state is unavailable.",
            )
        except Exception:
            return _error(
                500,
                RolloutApiError.INTERNAL_ERROR,
                "ROLLOUT_INTERNAL_ERROR",
                "The rollout could not be advanced.",
            )

    return app
