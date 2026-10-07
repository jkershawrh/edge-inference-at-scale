"""Independent evaluation attestation and release-signing authorization.

This module never loads a private key.  It prepares exact bytes for an external
Ed25519 signer, verifies the returned detached signature, and authorizes only
the evaluated candidate digest.  Production key custody remains outside the
repository in a KMS/HSM or equivalent protected signing service.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from corpus_factory.validator import ContractValidationError, validate_instance


SCHEMA_VERSION = "2.0.0"
GOVERNED_PROFILE = "governed-v1"
REQUIRED_APPROVAL_ROLES = {"evaluation_owner", "release_approver"}
REQUIRED_PROMOTION_LAYERS = {
    "corpus_suitability",
    "corpus_coverage",
    "release_validity",
    "retrieval",
    "grounded_answer",
    "edge_operation",
}
REQUIRED_BINDINGS = {
    "release_digest",
    "corpus_digest",
    "eval_set_digest",
    "model_digest",
    "embedding_model_digest",
    "chunker_digest",
    "policy_digest",
    "source_aggregate_digest",
    "document_aggregate_digest",
    "case_aggregate_digest",
}
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class EvaluationAttestationError(ValueError):
    """Raised when evaluation trust evidence fails closed."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationAttestationError(f"value is not canonical JSON: {exc}") from exc


def object_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise EvaluationAttestationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvaluationAttestationError(f"{label} must include a timezone")
    return parsed


def load_ed25519_public_key(value: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationAttestationError("attestation public key is not valid PEM") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise EvaluationAttestationError("attestation public key must be Ed25519")
    return key


def public_key_id(key: Ed25519PublicKey) -> str:
    der = key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def _validate_promotion_report(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping):
        raise EvaluationAttestationError("promotion report must be an object")
    report_id = report.get("report_id")
    body = {key: value for key, value in report.items() if key != "report_id"}
    if report_id != object_digest(body):
        raise EvaluationAttestationError("promotion report_id does not match its exact body")
    if report.get("contract_profile") != GOVERNED_PROFILE:
        raise EvaluationAttestationError("evaluation attestation requires governed-v1")
    if report.get("decision") != "PASS" or report.get("failures") != []:
        raise EvaluationAttestationError("only a passing promotion report can be attested")
    layers = report.get("layers")
    if not isinstance(layers, Mapping) or set(layers) != REQUIRED_PROMOTION_LAYERS:
        raise EvaluationAttestationError("promotion report does not contain every governed layer")
    if any(not isinstance(layer, Mapping) or layer.get("passed") is not True for layer in layers.values()):
        raise EvaluationAttestationError("every promotion layer must pass")
    binding = report.get("binding")
    if not isinstance(binding, Mapping) or set(binding) != REQUIRED_BINDINGS:
        raise EvaluationAttestationError("promotion report does not contain every immutable binding")
    if any(not isinstance(value, str) or not _DIGEST_RE.fullmatch(value) for value in binding.values()):
        raise EvaluationAttestationError("promotion report contains an invalid immutable binding")


def _approval_body(approval: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: approval[key]
        for key in (
            "identity",
            "role",
            "organization",
            "independence_group",
            "decision",
            "approved_at",
        )
    }


def _prepare_approvals(
    approvals: Sequence[Mapping[str, Any]],
    *,
    executor_identity: str,
    signer_identity: str,
    signed_at: datetime,
) -> list[Dict[str, Any]]:
    if isinstance(approvals, (str, bytes)) or not isinstance(approvals, Sequence):
        raise EvaluationAttestationError("approvals must be an array")
    prepared: list[Dict[str, Any]] = []
    for index, approval in enumerate(approvals):
        if not isinstance(approval, Mapping):
            raise EvaluationAttestationError(f"approval {index} must be an object")
        try:
            body = _approval_body(approval)
        except KeyError as exc:
            raise EvaluationAttestationError(
                f"approval {index} is missing {exc.args[0]}"
            ) from exc
        if body["decision"] != "approve":
            raise EvaluationAttestationError("every evaluation approval must approve")
        if body["role"] not in REQUIRED_APPROVAL_ROLES:
            raise EvaluationAttestationError("evaluation approval role is not permitted")
        if _parse_time(body["approved_at"], f"approval {index} approved_at") > signed_at:
            raise EvaluationAttestationError("approval cannot occur after signing")
        prepared.append({**body, "approval_digest": object_digest(body)})

    identities = [item["identity"] for item in prepared]
    groups = [item["independence_group"] for item in prepared]
    roles = {item["role"] for item in prepared}
    if roles != REQUIRED_APPROVAL_ROLES:
        raise EvaluationAttestationError(
            "evaluation_owner and release_approver approvals are both required"
        )
    if len(identities) != len(set(identities)):
        raise EvaluationAttestationError("approval identities must be independent")
    if len(groups) != len(set(groups)):
        raise EvaluationAttestationError("approval independence groups must be distinct")
    if executor_identity in identities:
        raise EvaluationAttestationError("evaluation executor cannot approve its own report")
    if signer_identity in identities or signer_identity == executor_identity:
        raise EvaluationAttestationError("signer, executor, and approvers must be distinct")
    return sorted(prepared, key=lambda item: (item["role"], item["identity"]))


def prepare_attestation_statement(
    promotion_report: Mapping[str, Any],
    approvals: Sequence[Mapping[str, Any]],
    *,
    evaluation_executor_identity: str,
    valid_from: str,
    expires_at: str,
    revocation_generation: int,
    revocation_checked_at: str,
    signer_identity: str,
    signed_at: str,
    public_key: Ed25519PublicKey,
    key_valid_from: str,
    key_expires_at: str,
) -> Dict[str, Any]:
    """Prepare the exact statement bytes an external signer must sign."""

    _validate_promotion_report(promotion_report)
    if not evaluation_executor_identity or not signer_identity:
        raise EvaluationAttestationError("executor and signer identities are required")
    if isinstance(revocation_generation, bool) or revocation_generation < 1:
        raise EvaluationAttestationError("revocation_generation must be a positive integer")

    valid_time = _parse_time(valid_from, "valid_from")
    expiry_time = _parse_time(expires_at, "expires_at")
    signed_time = _parse_time(signed_at, "signed_at")
    checked_time = _parse_time(revocation_checked_at, "revocation_checked_at")
    key_start = _parse_time(key_valid_from, "key_valid_from")
    key_end = _parse_time(key_expires_at, "key_expires_at")
    if not valid_time <= signed_time < expiry_time:
        raise EvaluationAttestationError("signing time must be inside attestation validity")
    if not key_start <= signed_time < key_end:
        raise EvaluationAttestationError("signing time must be inside key validity")
    if checked_time > signed_time:
        raise EvaluationAttestationError("revocation snapshot cannot postdate signing")

    prepared_approvals = _prepare_approvals(
        approvals,
        executor_identity=evaluation_executor_identity,
        signer_identity=signer_identity,
        signed_at=signed_time,
    )
    binding = promotion_report["binding"]
    body: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "evaluation_attestation",
        "subject": {
            "promotion_report_id": promotion_report["report_id"],
            "promotion_report_digest": object_digest(promotion_report),
            "release_digest": binding["release_digest"],
            "contract_profile": promotion_report["contract_profile"],
            "decision": promotion_report["decision"],
            "spec_version": promotion_report["spec_version"],
        },
        "evaluation_executor_identity": evaluation_executor_identity,
        "approvals": prepared_approvals,
        "lifecycle": {"valid_from": valid_from, "expires_at": expires_at},
        "revocation_snapshot": {
            "generation": revocation_generation,
            "checked_at": revocation_checked_at,
            "attestation_revoked_at": None,
            "attestation_revocation_reason": None,
            "key_revoked_at": None,
            "key_revocation_reason": None,
        },
        "signer": {
            "identity": signer_identity,
            "role": "signing_service",
            "signed_at": signed_at,
        },
        "signing_key": {
            "key_id": public_key_id(public_key),
            "role": "evaluation-attestation",
            "algorithm": "Ed25519",
            "valid_from": key_valid_from,
            "expires_at": key_expires_at,
        },
    }
    return {"attestation_id": object_digest(body), **body}


def signing_payload(statement: Mapping[str, Any]) -> bytes:
    if "signature" in statement:
        raise EvaluationAttestationError("signing statement cannot already contain a signature")
    return canonical_json(statement).encode("utf-8")


def finalize_attestation(
    statement: Mapping[str, Any], signature: bytes, public_key: Ed25519PublicKey
) -> Dict[str, Any]:
    if len(signature) != 64:
        raise EvaluationAttestationError("Ed25519 signature must be 64 bytes")
    payload = signing_payload(statement)
    try:
        public_key.verify(signature, payload)
    except InvalidSignature as exc:
        raise EvaluationAttestationError("evaluation attestation signature is invalid") from exc
    record = {**statement, "signature": {"value": base64.b64encode(signature).decode("ascii")}}
    try:
        validate_instance(record, "evaluation_attestation")
    except ContractValidationError as exc:
        raise EvaluationAttestationError(f"evaluation attestation contract failed: {exc}") from exc
    return record


def verify_attestation(
    attestation: Mapping[str, Any],
    promotion_report: Mapping[str, Any],
    public_key: Ed25519PublicKey,
    *,
    as_of: str,
    revoked_attestation_ids: Sequence[str] = (),
    revoked_key_ids: Sequence[str] = (),
) -> None:
    """Verify contract, identities, lifecycle, revocation, and signature."""

    try:
        validate_instance(attestation, "evaluation_attestation")
    except ContractValidationError as exc:
        raise EvaluationAttestationError(f"evaluation attestation contract failed: {exc}") from exc
    _validate_promotion_report(promotion_report)
    subject = attestation["subject"]
    expected = {
        "promotion_report_id": promotion_report["report_id"],
        "promotion_report_digest": object_digest(promotion_report),
        "release_digest": promotion_report["binding"]["release_digest"],
        "contract_profile": promotion_report["contract_profile"],
        "decision": promotion_report["decision"],
        "spec_version": promotion_report["spec_version"],
    }
    if subject != expected:
        raise EvaluationAttestationError("attestation subject does not match promotion report")
    if attestation["signing_key"]["key_id"] != public_key_id(public_key):
        raise EvaluationAttestationError("attestation signing key ID does not match public key")

    now = _parse_time(as_of, "as_of")
    if now < _parse_time(attestation["signer"]["signed_at"], "signed_at"):
        raise EvaluationAttestationError("trusted time predates attestation signing")
    lifecycle = attestation["lifecycle"]
    if not _parse_time(lifecycle["valid_from"], "valid_from") <= now < _parse_time(
        lifecycle["expires_at"], "expires_at"
    ):
        raise EvaluationAttestationError("evaluation attestation is not currently valid")
    key = attestation["signing_key"]
    if not _parse_time(key["valid_from"], "key_valid_from") <= now < _parse_time(
        key["expires_at"], "key_expires_at"
    ):
        raise EvaluationAttestationError("evaluation attestation key is not currently valid")
    snapshot = attestation["revocation_snapshot"]
    if snapshot["attestation_revoked_at"] is not None:
        raise EvaluationAttestationError("evaluation attestation is revoked")
    if snapshot["key_revoked_at"] is not None:
        raise EvaluationAttestationError("evaluation attestation key is revoked")
    if attestation["attestation_id"] in set(revoked_attestation_ids):
        raise EvaluationAttestationError("evaluation attestation appears in revocation state")
    if key["key_id"] in set(revoked_key_ids):
        raise EvaluationAttestationError("evaluation attestation key appears in revocation state")

    statement = {key: value for key, value in attestation.items() if key != "signature"}
    body = {key: value for key, value in statement.items() if key != "attestation_id"}
    if statement["attestation_id"] != object_digest(body):
        raise EvaluationAttestationError("attestation_id does not match statement body")
    try:
        signature = base64.b64decode(attestation["signature"]["value"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EvaluationAttestationError("evaluation attestation signature is malformed") from exc
    try:
        public_key.verify(signature, signing_payload(statement))
    except InvalidSignature as exc:
        raise EvaluationAttestationError("evaluation attestation signature is invalid") from exc


def authorize_release_signing(
    candidate_manifest: bytes,
    promotion_report: Mapping[str, Any],
    attestation: Mapping[str, Any],
    public_key: Ed25519PublicKey,
    *,
    as_of: str,
    revoked_attestation_ids: Sequence[str] = (),
    revoked_key_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Authorize an external release signer for one exact candidate digest."""

    verify_attestation(
        attestation,
        promotion_report,
        public_key,
        as_of=as_of,
        revoked_attestation_ids=revoked_attestation_ids,
        revoked_key_ids=revoked_key_ids,
    )
    digest = bytes_digest(candidate_manifest)
    if digest != promotion_report["binding"]["release_digest"]:
        raise EvaluationAttestationError("candidate manifest differs from evaluated release digest")
    authorization: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "record_type": "release_signing_authorization",
        "candidate_release_digest": digest,
        "promotion_report": {
            "report_id": promotion_report["report_id"],
            "digest": object_digest(promotion_report),
        },
        "evaluation_attestation": {
            "attestation_id": attestation["attestation_id"],
            "digest": object_digest(attestation),
            "key_id": attestation["signing_key"]["key_id"],
            "revocation_generation": attestation["revocation_snapshot"]["generation"],
        },
        "release_signature_bindings": {
            "evaluation_report_digest": object_digest(promotion_report),
            "approval_attestation_digests": [object_digest(attestation)],
        },
        "authorization": {
            "action": "sign_exact_candidate_digest",
            "required_key_role": "corpus-release",
            "authorized_at": as_of,
            "private_key_available": False,
        },
    }
    authorization["authorization_id"] = object_digest(authorization)
    try:
        validate_instance(authorization, "release_signing_authorization")
    except ContractValidationError as exc:
        raise EvaluationAttestationError(f"signing authorization contract failed: {exc}") from exc
    return authorization


def load_json_object(path: Path | str) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationAttestationError(f"cannot read JSON object from {path}") from exc
    if not isinstance(value, dict):
        raise EvaluationAttestationError(f"{path} must contain a JSON object")
    return value
