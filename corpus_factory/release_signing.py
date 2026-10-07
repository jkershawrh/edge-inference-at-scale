"""Protected release-signing boundary with no private-key import or export path."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from corpus_factory.evaluation_attestation import bytes_digest, canonical_json, object_digest
from corpus_factory.validator import ContractValidationError, validate_instance


class ReleaseSigningError(ValueError):
    """Raised when a signing request cannot cross the protected boundary."""


@dataclass(frozen=True)
class SigningKeyMetadata:
    key_id: str
    role: str
    algorithm: str
    generation: int
    rotation_state: str
    revocation_generation: int
    valid_from: str
    expires_at: str
    public_key_spki: bytes


class ProtectedSignerAdapter(Protocol):
    """Minimal PKCS#11/KMS contract; deliberately has no key export method."""

    def describe_key(self, key_id: str) -> SigningKeyMetadata: ...

    def sign(self, key_id: str, payload: bytes) -> bytes: ...


@dataclass(frozen=True)
class ReleaseSigningPolicy:
    allowed_key_ids: tuple[str, ...]
    trusted_attestation_key_ids: tuple[str, ...]
    minimum_key_revocation_generation: int
    minimum_attestation_revocation_generation: int
    maximum_authorization_age_seconds: int = 86400
    maximum_candidate_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.allowed_key_ids or not self.trusted_attestation_key_ids:
            raise ValueError("signing and attestation key allowlists cannot be empty")
        if len(set(self.allowed_key_ids)) != len(self.allowed_key_ids):
            raise ValueError("allowed_key_ids must be unique")
        if self.minimum_key_revocation_generation < 1 or self.minimum_attestation_revocation_generation < 1:
            raise ValueError("revocation generation floors must be positive")
        if not 1 <= self.maximum_authorization_age_seconds <= 7 * 86400:
            raise ValueError("maximum authorization age must be between 1 second and 7 days")
        if not 1 <= self.maximum_candidate_bytes <= 16 * 1024 * 1024:
            raise ValueError("maximum candidate size must be between 1 byte and 16 MiB")


def _time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ReleaseSigningError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ReleaseSigningError(f"{label} must include a timezone")
    return parsed


def _public_key(metadata: SigningKeyMetadata) -> Ed25519PublicKey:
    try:
        value = serialization.load_der_public_key(metadata.public_key_spki)
    except (TypeError, ValueError) as exc:
        raise ReleaseSigningError("signing adapter returned an invalid public key") from exc
    if not isinstance(value, Ed25519PublicKey):
        raise ReleaseSigningError("release signing key must be Ed25519")
    return value


def signing_payload(statement: Mapping[str, Any]) -> bytes:
    if "signature" in statement:
        raise ReleaseSigningError("release signature statement already contains a signature")
    return canonical_json(statement).encode("utf-8")


class SigningReplayStore:
    """Durable one-authorization/one-key replay and conflict guard."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS signatures (authorization_id TEXT NOT NULL, key_id TEXT NOT NULL, request_digest TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY (authorization_id, key_id))"
            )

    def lookup(self, authorization_id: str, key_id: str, request_digest: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT request_digest, result_json FROM signatures WHERE authorization_id=? AND key_id=?",
                (authorization_id, key_id),
            ).fetchone()
        if row is None:
            return None
        if row[0] != request_digest:
            raise ReleaseSigningError("authorization and key were already used for a different request")
        return json.loads(row[1])

    def save(self, authorization_id: str, key_id: str, request_digest: str, result: Mapping[str, Any]) -> None:
        encoded = canonical_json(result)
        try:
            with sqlite3.connect(self.path) as connection:
                connection.execute(
                    "INSERT INTO signatures VALUES (?, ?, ?, ?)",
                    (authorization_id, key_id, request_digest, encoded),
                )
        except sqlite3.IntegrityError:
            existing = self.lookup(authorization_id, key_id, request_digest)
            if existing != result:
                raise ReleaseSigningError("conflicting concurrent signing replay")


class ReleaseSigningService:
    def __init__(self, adapter: ProtectedSignerAdapter, policy: ReleaseSigningPolicy, replay_store: SigningReplayStore):
        self._adapter = adapter
        self._policy = policy
        self._replay_store = replay_store

    def sign(self, candidate: bytes, authorization: Mapping[str, Any], *, key_id: str, signed_at: str) -> dict[str, Any]:
        if not isinstance(candidate, bytes) or not candidate or len(candidate) > self._policy.maximum_candidate_bytes:
            raise ReleaseSigningError("candidate is absent or exceeds the configured size limit")
        try:
            validate_instance(authorization, "release_signing_authorization")
        except ContractValidationError as exc:
            raise ReleaseSigningError(f"release signing authorization is invalid: {exc}") from exc
        if bytes_digest(candidate) != authorization["candidate_release_digest"]:
            raise ReleaseSigningError("candidate bytes do not match the authorized digest")
        if key_id not in self._policy.allowed_key_ids:
            raise ReleaseSigningError("requested signing key is not allowlisted")
        attestation = authorization["evaluation_attestation"]
        if attestation["key_id"] not in self._policy.trusted_attestation_key_ids:
            raise ReleaseSigningError("authorization names an untrusted attestation key")
        if attestation["revocation_generation"] < self._policy.minimum_attestation_revocation_generation:
            raise ReleaseSigningError("authorization attestation revocation state is stale")

        signing_time = _time(signed_at, "signed_at")
        authorized_time = _time(authorization["authorization"]["authorized_at"], "authorized_at")
        age = signing_time - authorized_time
        if age < timedelta(0) or age > timedelta(seconds=self._policy.maximum_authorization_age_seconds):
            raise ReleaseSigningError("release signing authorization is not current")

        metadata = self._adapter.describe_key(key_id)
        if metadata.key_id != key_id or metadata.role != "corpus-release" or metadata.algorithm != "Ed25519":
            raise ReleaseSigningError("signing key metadata violates the required role or algorithm")
        if metadata.rotation_state not in {"active", "overlap"}:
            raise ReleaseSigningError("signing key is retired or revoked")
        if metadata.generation < 1 or metadata.revocation_generation < self._policy.minimum_key_revocation_generation:
            raise ReleaseSigningError("signing key revocation state is stale")
        if not _time(metadata.valid_from, "key valid_from") <= signing_time < _time(metadata.expires_at, "key expires_at"):
            raise ReleaseSigningError("signing key is outside its validity window")
        public_key = _public_key(metadata)

        key_record = {key: value for key, value in asdict(metadata).items() if key != "public_key_spki"}
        body: dict[str, Any] = {
            "schema_version": "1.0.0",
            "record_type": "release_signature",
            "candidate_release_digest": authorization["candidate_release_digest"],
            "authorization": {
                "authorization_id": authorization["authorization_id"],
                "digest": object_digest(authorization),
            },
            "governance_bindings": dict(authorization["release_signature_bindings"]),
            "signing_key": key_record,
            "signed_at": signed_at,
        }
        statement = {"signature_id": object_digest(body), **body}
        request_digest = object_digest(statement)
        replayed = self._replay_store.lookup(authorization["authorization_id"], key_id, request_digest)
        if replayed is not None:
            return replayed
        signature = self._adapter.sign(key_id, signing_payload(statement))
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise ReleaseSigningError("signing adapter returned an invalid Ed25519 signature")
        try:
            public_key.verify(signature, signing_payload(statement))
        except InvalidSignature as exc:
            raise ReleaseSigningError("signing adapter returned an unverifiable signature") from exc
        manifest_signature = self._adapter.sign(key_id, candidate)
        if not isinstance(manifest_signature, bytes) or len(manifest_signature) != 64:
            raise ReleaseSigningError("signing adapter returned an invalid manifest signature")
        try:
            public_key.verify(manifest_signature, candidate)
        except InvalidSignature as exc:
            raise ReleaseSigningError("signing adapter returned an unverifiable manifest signature") from exc
        result = {
            **statement,
            "signature": {"encoding": "base64", "value": base64.b64encode(signature).decode("ascii")},
            "manifest_signature": {"encoding": "base64", "value": base64.b64encode(manifest_signature).decode("ascii")},
        }
        try:
            validate_instance(result, "release_signature")
        except ContractValidationError as exc:
            raise ReleaseSigningError(f"release signature contract failed: {exc}") from exc
        self._replay_store.save(authorization["authorization_id"], key_id, request_digest, result)
        return result


def verify_release_signature(candidate: bytes, authorization: Mapping[str, Any], signature_record: Mapping[str, Any], public_key: Ed25519PublicKey) -> None:
    try:
        validate_instance(authorization, "release_signing_authorization")
        validate_instance(signature_record, "release_signature")
    except ContractValidationError as exc:
        raise ReleaseSigningError(f"release signature evidence is invalid: {exc}") from exc
    if bytes_digest(candidate) != signature_record["candidate_release_digest"] or authorization["candidate_release_digest"] != signature_record["candidate_release_digest"]:
        raise ReleaseSigningError("candidate does not match the release signature")
    if signature_record["authorization"] != {"authorization_id": authorization["authorization_id"], "digest": object_digest(authorization)}:
        raise ReleaseSigningError("release signature does not bind the supplied authorization")
    statement = {
        key: value
        for key, value in signature_record.items()
        if key not in {"signature", "manifest_signature"}
    }
    try:
        signature = base64.b64decode(signature_record["signature"]["value"], validate=True)
        public_key.verify(signature, signing_payload(statement))
        manifest_signature = base64.b64decode(signature_record["manifest_signature"]["value"], validate=True)
        public_key.verify(manifest_signature, candidate)
    except (binascii.Error, InvalidSignature) as exc:
        raise ReleaseSigningError("release signature is invalid") from exc
