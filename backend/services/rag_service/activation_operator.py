"""Standalone HTTP boundary for authenticated Lil EVY corpus activation."""
from __future__ import annotations

import json
from enum import Enum
from typing import Annotated, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from .activation_contracts import (
    ActivationAcceptedResponse,
    ActivationCandidateRequest,
    ActivationReceiptResponse,
    ActivationResult,
    StrictContract,
)
from .activation_control import (
    ActivationAuthenticationError,
    ActivationControl,
    ActivationPathError,
)


DEFAULT_MAX_REQUEST_BYTES = 8 * 1024


class OperatorError(str, Enum):
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    REQUEST_TOO_LARGE = "request_too_large"
    INVALID_REQUEST = "invalid_request"
    ACTIVATION_CONFLICT = "activation_conflict"
    INTERNAL_ERROR = "internal_error"


class OperatorErrorResponse(StrictContract):
    error: OperatorError
    reason_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")]
    message: str = Field(min_length=1, max_length=160)
    receipt: Optional[ActivationReceiptResponse] = None


class OperatorHealthResponse(StrictContract):
    service: Literal["lil-evy-activation-operator"]
    status: Literal["ready"]
    version: Literal["1"]


class _RequestTooLarge(ValueError):
    pass


def _error(
    status_code: int,
    error: OperatorError,
    reason_code: str,
    message: str,
    *,
    receipt: Optional[ActivationReceiptResponse] = None,
    authenticate: bool = False,
) -> JSONResponse:
    response = OperatorErrorResponse(
        error=error,
        reason_code=reason_code,
        message=message,
        receipt=receipt,
    )
    headers = {"WWW-Authenticate": "Bearer"} if authenticate else None
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json"),
        headers=headers,
    )


def _authorization_header(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise ValueError("authorization header is required exactly once")
    value = values[0]
    if not value.startswith("Bearer ") or len(value) <= len("Bearer "):
        raise ValueError("authorization header must use the Bearer scheme")
    token = value[len("Bearer "):]
    if any(character.isspace() or ord(character) < 0x21 or ord(character) > 0x7E
           for character in token):
        raise ValueError("authorization bearer token is malformed")
    return value


async def _bounded_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ValueError("content-length is invalid") from exc
        if declared_length > maximum:
            raise _RequestTooLarge

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise _RequestTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def create_activation_operator_app(
    control: ActivationControl,
    *,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> FastAPI:
    """Create an isolated operator app around an injected activation control."""

    if not isinstance(max_request_bytes, int) or not 256 <= max_request_bytes <= 64 * 1024:
        raise ValueError("max_request_bytes must be between 256 and 65536")

    app = FastAPI(
        title="Lil EVY Activation Operator",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health", response_model=OperatorHealthResponse)
    async def health() -> OperatorHealthResponse:
        return OperatorHealthResponse(
            service="lil-evy-activation-operator", status="ready", version="1"
        )

    @app.post("/v1/activation", response_model=ActivationAcceptedResponse, status_code=202)
    async def activate(request: Request) -> ActivationAcceptedResponse | JSONResponse:
        try:
            authorization = _authorization_header(request)
        except ValueError:
            return _error(
                401,
                OperatorError.UNAUTHORIZED,
                "INVALID_AUTHORIZATION",
                "A single valid Bearer authorization header is required.",
                authenticate=True,
            )

        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != "application/json":
            return _error(
                422,
                OperatorError.INVALID_REQUEST,
                "INVALID_CONTENT_TYPE",
                "The request content type must be application/json.",
            )

        try:
            raw_body = await _bounded_body(request, max_request_bytes)
        except _RequestTooLarge:
            return _error(
                413,
                OperatorError.REQUEST_TOO_LARGE,
                "REQUEST_TOO_LARGE",
                "The activation request exceeds the configured size limit.",
            )
        except ValueError:
            return _error(
                422,
                OperatorError.INVALID_REQUEST,
                "INVALID_REQUEST",
                "The activation request is invalid.",
            )

        try:
            payload = json.loads(raw_body)
            candidate = ActivationCandidateRequest.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError):
            return _error(
                422,
                OperatorError.INVALID_REQUEST,
                "INVALID_REQUEST",
                "The activation request is invalid.",
            )

        try:
            receipt = control.activate(candidate, authorization)
        except ActivationAuthenticationError:
            return _error(
                403,
                OperatorError.FORBIDDEN,
                "CREDENTIAL_REJECTED",
                "The activation credential was not accepted.",
            )
        except ActivationPathError:
            return _error(
                422,
                OperatorError.INVALID_REQUEST,
                "INVALID_PACKAGE_PATH",
                "The package path is not an eligible local intake package.",
            )
        except Exception:
            return _error(
                500,
                OperatorError.INTERNAL_ERROR,
                "ACTIVATION_INTERNAL_ERROR",
                "The activation request could not be completed.",
            )

        if receipt.result is not ActivationResult.SUCCESS:
            return _error(
                409,
                OperatorError.ACTIVATION_CONFLICT,
                receipt.reason_code,
                "The candidate was not activated.",
                receipt=receipt,
            )
        return ActivationAcceptedResponse(receipt=receipt)

    return app
