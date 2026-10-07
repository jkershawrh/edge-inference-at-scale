"""Authenticated, bounded HTTP boundary for an injected protected signer."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from corpus_factory.release_signing import ReleaseSigningError, ReleaseSigningService


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


async def _body(request: Request, maximum: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > maximum:
                raise ValueError
        except ValueError as exc:
            raise ReleaseSigningError("request exceeds size limit") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise ReleaseSigningError("request exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def create_release_signing_app(service: ReleaseSigningService, *, bearer_token: str, max_request_bytes: int = 6 * 1024 * 1024) -> FastAPI:
    if not isinstance(bearer_token, str) or not 32 <= len(bearer_token) <= 512 or not bearer_token.isprintable():
        raise ValueError("bearer_token must be 32-512 printable characters")
    if not 1024 <= max_request_bytes <= 20 * 1024 * 1024:
        raise ValueError("max_request_bytes must be between 1 KiB and 20 MiB")
    app = FastAPI(title="Big EVY Protected Release Signer", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"service": "big-evy-release-signer", "status": "ready", "version": "1"}

    @app.post("/v1/sign-release", response_model=None)
    async def sign_release(request: Request) -> dict[str, Any] | JSONResponse:
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not values[0].startswith("Bearer ") or not secrets.compare_digest(values[0][7:], bearer_token):
            return _error(401, "UNAUTHORIZED", "A valid signer credential is required.")
        if request.headers.get("content-type", "").partition(";")[0].lower() != "application/json":
            return _error(422, "INVALID_CONTENT_TYPE", "The request content type must be application/json.")
        try:
            payload = json.loads(await _body(request, max_request_bytes))
            if not isinstance(payload, dict) or set(payload) != {"candidate_base64", "authorization", "promotion_report", "evaluation_attestation", "key_id"}:
                raise ValueError
            if not all(isinstance(payload[field], str) for field in ("candidate_base64", "key_id")) or not all(
                isinstance(payload[field], dict)
                for field in ("authorization", "promotion_report", "evaluation_attestation")
            ):
                raise ValueError
            candidate = base64.b64decode(payload["candidate_base64"], validate=True)
        except ReleaseSigningError:
            return _error(413, "REQUEST_TOO_LARGE", "The signing request exceeds the configured limit.")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError, binascii.Error):
            return _error(422, "INVALID_REQUEST", "The signing request is invalid.")
        try:
            return service.sign(
                candidate,
                payload["authorization"],
                payload["promotion_report"],
                payload["evaluation_attestation"],
                key_id=payload["key_id"],
            )
        except ReleaseSigningError:
            return _error(409, "SIGNING_REJECTED", "The release signing policy rejected the request.")
        except Exception:
            return _error(500, "SIGNING_UNAVAILABLE", "The protected signing provider is unavailable.")

    return app
