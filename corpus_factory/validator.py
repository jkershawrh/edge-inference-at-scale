"""Strict validation for the version 2 Big EVY/Lil EVY data contract.

JSON Schema handles record shape.  Small semantic checks here cover invariants
that JSON Schema cannot express clearly, such as digest correctness and ordered
time windows.  This module performs no network or runtime-service imports.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union
from urllib.parse import urlparse

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError as exc:  # pragma: no cover - exercised only in minimal installs
    raise RuntimeError("Corpus contract validation requires the 'jsonschema' package") from exc


SCHEMA_DIR = Path(__file__).with_name("schemas")
SCHEMA_BY_RECORD_TYPE = {
    "corpus_mission_profile": "corpus-mission-profile.schema.json",
    "document_classification": "document-classification.schema.json",
    "coverage_report": "coverage-report.schema.json",
    "source_record": "source-record.schema.json",
    "canonical_document": "canonical-document.schema.json",
    "chunk_record": "chunk-record.schema.json",
    "review_attestation": "review-attestation.schema.json",
    "evaluation_attestation": "evaluation-attestation.schema.json",
    "release_signing_authorization": "release-signing-authorization.schema.json",
    "corpus_audit_checkpoint": "audit-checkpoint.schema.json",
    "corpus_audit_anchor_receipt": "audit-anchor-receipt.schema.json",
    "release_manifest": "release-manifest.schema.json",
    "activation_receipt": "activation-receipt.schema.json",
    "event_policy": "event-policy.schema.json",
    "source_registry": "source-registry.schema.json",
    "acquisition_report": "acquisition-report.schema.json",
    "refresh_plan": "refresh-plan.schema.json",
}


class ContractValidationError(ValueError):
    """Raised when a v2 contract record is malformed or semantically unsafe."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError("duplicate JSON key: {0}".format(key))
        result[key] = value
    return result


def load_json(path: Union[Path, str]) -> Dict[str, Any]:
    """Load UTF-8 JSON while rejecting ambiguous duplicate object keys."""

    source = Path(path)
    try:
        value = json.loads(
            source.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError("invalid JSON in {0}: {1}".format(source, exc)) from exc
    if not isinstance(value, dict):
        raise ContractValidationError("contract record must be a JSON object")
    return value


def load_schema(record_type: str) -> Dict[str, Any]:
    """Return and self-check the standalone schema for a record type."""

    filename = SCHEMA_BY_RECORD_TYPE.get(record_type)
    if filename is None:
        raise ContractValidationError("unknown record_type: {0}".format(record_type))
    schema = load_json(SCHEMA_DIR / filename)
    Draft202012Validator.check_schema(schema)
    return schema


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ordered_time_window(
    record: Mapping[str, Any], start_name: str, end_name: str, label: str
) -> None:
    start = _parse_time(record.get(start_name))
    end = _parse_time(record.get(end_name))
    if start is not None and end is not None and end <= start:
        raise ContractValidationError("{0}: {1} must be after {2}".format(label, end_name, start_name))


def event_policy_subject_digest(instance: Mapping[str, Any]) -> str:
    """Digest the approved policy body without its self-referencing approvals."""

    body = {key: value for key, value in instance.items() if key != "human_approval_attestations"}
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _semantic_validate(instance: Mapping[str, Any]) -> None:
    record_type = instance["record_type"]
    if record_type == "corpus_mission_profile":
        event = instance["event"]
        _ordered_time_window(event, "starts_at", "ends_at", "mission event")
        validity = instance["validity"]
        _ordered_time_window(validity, "valid_from", "valid_until", "mission validity")
        requirements = instance["required_information"]
        requirement_ids = [item["requirement_id"] for item in requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ContractValidationError("mission requirement IDs must be unique")
        category_ids = [item["category_id"] for item in requirements]
        if len(category_ids) != len(set(category_ids)):
            raise ContractValidationError("mission coverage category IDs must be unique")
    elif record_type == "document_classification":
        validity = instance["validity"]
        _ordered_time_window(validity, "valid_from", "valid_until", "classification validity")
        verification = instance["verification"]
        is_verified = verification["status"] == "verified"
        has_verifier = (
            verification["verified_at"] is not None
            and verification["reviewer_identity"] is not None
        )
        if instance["answer_policy"]["direct_answer_eligible"] and not is_verified:
            raise ContractValidationError(
                "direct-answer eligibility requires verified evidence"
            )
        if is_verified != has_verifier:
            raise ContractValidationError(
                "verified classification requires verification time and reviewer"
            )
        if (
            instance["risk"]["safety_class"] == "critical"
            and validity["stale_action"] != "block"
        ):
            raise ContractValidationError("critical classification must block when stale")
        if (
            instance["authority"]["class"] == "unverified"
            and instance["answer_policy"]["direct_answer_eligible"]
        ):
            raise ContractValidationError(
                "unverified authority cannot be direct-answer eligible"
            )
    elif record_type == "coverage_report":
        body = {key: value for key, value in instance.items() if key != "report_id"}
        expected = "sha256:" + hashlib.sha256(
            json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        if instance["report_id"] != expected:
            raise ContractValidationError(
                "coverage report_id does not match report body"
            )
        requirements = instance["requirements"]
        requirement_ids = [item["requirement_id"] for item in requirements]
        category_ids = [item["category_id"] for item in requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ContractValidationError(
                "coverage report requirement IDs must be unique"
            )
        if len(category_ids) != len(set(category_ids)):
            raise ContractValidationError(
                "coverage report category IDs must be unique"
            )
        statuses = [item["status"] for item in requirements]
        expected_summary = {
            "requirements": len(statuses),
            "covered": statuses.count("COVERED"),
            "gaps": statuses.count("GAP"),
            "conflicted": statuses.count("CONFLICTED"),
        }
        if instance["summary"] != expected_summary:
            raise ContractValidationError(
                "coverage report summary does not match requirements"
            )
        expected_decision = (
            "CONFLICTS"
            if "CONFLICTED" in statuses
            else ("GAPS" if "GAP" in statuses else "COVERED")
        )
        if instance["decision"] != expected_decision:
            raise ContractValidationError(
                "coverage report decision does not match requirements"
            )
    elif record_type == "source_record":
        acquired = _parse_time(instance["acquired_at"])
        verified = _parse_time(instance["last_verified_at"])
        if verified < acquired:
            raise ContractValidationError("last_verified_at cannot precede acquired_at")
        _ordered_time_window(instance["freshness"], "effective_from", "valid_until", "freshness")
    elif record_type == "canonical_document":
        expected_digest = "sha256:" + hashlib.sha256(
            instance["canonical_text"].encode("utf-8")
        ).hexdigest()
        if instance["document_digest"] != expected_digest:
            raise ContractValidationError(
                "document_digest does not match canonical_text"
            )
        document_sources = {
            (citation["source_id"], citation["evidence_digest"])
            for citation in instance["source_citations"]
        }
        for fact in instance["structured_facts"]:
            for citation in fact["citations"]:
                if (citation["source_id"], citation["evidence_digest"]) not in document_sources:
                    raise ContractValidationError(
                        "structured fact citation is absent from document source lineage"
                    )
        validity = instance["validity"]
        _ordered_time_window(validity, "valid_from", "valid_until", "validity")
        valid_from = _parse_time(validity["valid_from"])
        review_due = _parse_time(validity["review_due_at"])
        if review_due < valid_from:
            raise ContractValidationError("review_due_at cannot precede valid_from")
    elif record_type == "chunk_record":
        span = instance["source_span"]
        if span["end"] <= span["start"]:
            raise ContractValidationError("source_span.end must be greater than source_span.start")
        expected = "sha256:" + hashlib.sha256(instance["content"].encode("utf-8")).hexdigest()
        if instance["content_digest"] != expected:
            raise ContractValidationError("chunk content_digest does not match UTF-8 content")
    elif record_type == "review_attestation":
        local = instance["local_validation"]
        if local["required"] and not local["completed"]:
            raise ContractValidationError("required local validation is incomplete")
    elif record_type == "evaluation_attestation":
        lifecycle = instance["lifecycle"]
        _ordered_time_window(lifecycle, "valid_from", "expires_at", "attestation lifecycle")
        key = instance["signing_key"]
        _ordered_time_window(key, "valid_from", "expires_at", "attestation key")
        signed_at = _parse_time(instance["signer"]["signed_at"])
        valid_from = _parse_time(lifecycle["valid_from"])
        expires_at = _parse_time(lifecycle["expires_at"])
        key_valid_from = _parse_time(key["valid_from"])
        key_expires_at = _parse_time(key["expires_at"])
        if not valid_from <= signed_at < expires_at:
            raise ContractValidationError("signing time must be inside attestation validity")
        if not key_valid_from <= signed_at < key_expires_at:
            raise ContractValidationError("signing time must be inside attestation key validity")
        snapshot = instance["revocation_snapshot"]
        if _parse_time(snapshot["checked_at"]) > signed_at:
            raise ContractValidationError("revocation snapshot cannot postdate signing")
        for prefix in ("attestation", "key"):
            revoked = snapshot[f"{prefix}_revoked_at"]
            reason = snapshot[f"{prefix}_revocation_reason"]
            if (revoked is None) != (reason is None):
                raise ContractValidationError(
                    f"{prefix} revocation time and reason must be set together"
                )
        identities = [item["identity"] for item in instance["approvals"]]
        groups = [item["independence_group"] for item in instance["approvals"]]
        roles = {item["role"] for item in instance["approvals"]}
        if len(identities) != len(set(identities)) or len(groups) != len(set(groups)):
            raise ContractValidationError("evaluation approvals must be independent")
        if roles != {"evaluation_owner", "release_approver"}:
            raise ContractValidationError(
                "evaluation_owner and release_approver approvals are required"
            )
        protected = set(identities)
        protected.add(instance["evaluation_executor_identity"])
        if instance["signer"]["identity"] in protected:
            raise ContractValidationError("signer, executor, and approvers must be distinct")
        if instance["evaluation_executor_identity"] in identities:
            raise ContractValidationError("evaluation executor cannot approve its own report")
        for approval in instance["approvals"]:
            approved_at = _parse_time(approval["approved_at"])
            if not valid_from <= approved_at <= signed_at:
                raise ContractValidationError(
                    "evaluation approval must occur during validity and before signing"
                )
            body = {key: value for key, value in approval.items() if key != "approval_digest"}
            expected = "sha256:" + hashlib.sha256(
                json.dumps(
                    body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            ).hexdigest()
            if approval["approval_digest"] != expected:
                raise ContractValidationError("evaluation approval digest does not match body")
        body = {
            key: value
            for key, value in instance.items()
            if key not in {"attestation_id", "signature"}
        }
        expected_id = "sha256:" + hashlib.sha256(
            json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        if instance["attestation_id"] != expected_id:
            raise ContractValidationError("attestation_id does not match statement body")
    elif record_type == "release_manifest":
        lifecycle = instance["lifecycle"]
        _ordered_time_window(lifecycle, "created_at", "effective_at", "release lifecycle")
        _ordered_time_window(lifecycle, "effective_at", "expires_at", "release lifecycle")
        if (lifecycle["revoked_at"] is None) != (lifecycle["revocation_reason"] is None):
            raise ContractValidationError("revoked_at and revocation_reason must be set together")
        if instance["sequence"] == 1 and instance["previous_release_digest"] is not None:
            raise ContractValidationError("the first sequence cannot name a previous release")
        embedding = instance["derivation"]["embedding"]
        embedding_count = instance["counts"]["embeddings"]
        if (embedding is None) != (embedding_count == 0):
            raise ContractValidationError("embedding metadata and count disagree")
        layer_names = [layer["name"] for layer in instance["layers"]]
        if len(layer_names) != len(set(layer_names)):
            raise ContractValidationError("release layer names must be unique")
    elif record_type == "release_signing_authorization":
        body = {key: value for key, value in instance.items() if key != "authorization_id"}
        expected = "sha256:" + hashlib.sha256(
            json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        if instance["authorization_id"] != expected:
            raise ContractValidationError("authorization_id does not match authorization body")
    elif record_type == "corpus_audit_checkpoint":
        previous = instance["previous_anchor"]
        if (previous["receipt_id"] is None) != (previous["receipt_digest"] is None):
            raise ContractValidationError("previous anchor ID and digest must be set together")
        if instance["checkpoint_sequence"] == 1 and previous["receipt_id"] is not None:
            raise ContractValidationError("first checkpoint cannot name a previous anchor")
        if instance["checkpoint_sequence"] > 1 and previous["receipt_id"] is None:
            raise ContractValidationError("later checkpoint requires a previous anchor")
        body = {key: value for key, value in instance.items() if key != "checkpoint_id"}
        expected = "sha256:" + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if instance["checkpoint_id"] != expected:
            raise ContractValidationError("checkpoint_id does not match checkpoint body")
    elif record_type == "corpus_audit_anchor_receipt":
        checkpoint = instance["checkpoint"]
        _semantic_validate(checkpoint)
        created_at = _parse_time(checkpoint["created_at"])
        signed_at = _parse_time(instance["witness"]["signed_at"])
        key = instance["signing_key"]
        key_start = _parse_time(key["valid_from"])
        key_end = _parse_time(key["expires_at"])
        checked_at = _parse_time(instance["revocation_snapshot"]["checked_at"])
        if signed_at < created_at:
            raise ContractValidationError("witness signature cannot predate checkpoint")
        if not key_start <= signed_at < key_end:
            raise ContractValidationError("witness signing time must be inside key validity")
        if checked_at > signed_at:
            raise ContractValidationError("witness revocation snapshot cannot postdate signing")
        if instance["witness"]["identity"] == checkpoint["requester_identity"]:
            raise ContractValidationError("external witness must be independent of requester")
        snapshot = instance["revocation_snapshot"]
        if (snapshot["key_revoked_at"] is None) != (snapshot["key_revocation_reason"] is None):
            raise ContractValidationError("witness key revocation time and reason must be set together")
        body = {key: value for key, value in instance.items() if key not in {"receipt_id", "signature"}}
        expected = "sha256:" + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if instance["receipt_id"] != expected:
            raise ContractValidationError("receipt_id does not match witness statement body")
    elif record_type == "activation_receipt" and instance["result"] == "success":
        if instance["activated"] != instance["desired"]:
            raise ContractValidationError("successful activation must activate the desired release")
        checks = instance["verification"]
        if not all(value for key, value in checks.items() if key.endswith("_verified")):
            raise ContractValidationError("successful activation requires every verification check")
        if not instance["smoke_tests"]["passed"]:
            raise ContractValidationError("successful activation requires passing smoke tests")
        if instance["transition"]["to"] != "ACTIVE":
            raise ContractValidationError("successful activation must transition to ACTIVE")
    elif record_type == "event_policy":
        deployment_scope = instance["deployment_scope"]
        authority_ids = [entry["authority_id"] for entry in instance["authority_registry"]]
        if len(authority_ids) != len(set(authority_ids)):
            raise ContractValidationError("event policy authority IDs must be unique")
        source_ids = [
            source_id
            for entry in instance["authority_registry"]
            for source_id in entry["source_ids"]
        ]
        if len(source_ids) != len(set(source_ids)):
            raise ContractValidationError("event policy source IDs cannot have ambiguous authorities")

        requirement_ids = [
            requirement["requirement_id"] for requirement in instance["coverage_requirements"]
        ]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ContractValidationError("event policy requirement IDs must be unique")
        scope_fields = ("geographies", "languages", "audiences", "delivery_channels")
        for requirement in instance["coverage_requirements"]:
            for field in scope_fields:
                if not set(requirement["scope"][field]).issubset(deployment_scope[field]):
                    raise ContractValidationError(
                        "requirement {0} {1} exceeds deployment scope".format(
                            requirement["requirement_id"], field
                        )
                    )

        approvals = instance["human_approval_attestations"]
        expected_digest = event_policy_subject_digest(instance)
        if any(item["policy_digest"] != expected_digest for item in approvals):
            raise ContractValidationError("policy approval digest does not match policy body")
        for field in ("attestation_id", "reviewer_identity", "reviewer_role", "independence_group"):
            values = [item[field] for item in approvals]
            if len(values) != len(set(values)):
                raise ContractValidationError("policy approvals require unique {0}".format(field))
        if any(item["safety_class"] == "critical" for item in instance["coverage_requirements"]):
            roles = {item["reviewer_role"] for item in approvals}
            if not {"domain_sme", "local_sme"}.issubset(roles):
                raise ContractValidationError(
                    "critical event policy requires domain_sme and local_sme approvals"
                )
        no_answer = instance["no_answer_policy"]
        if not no_answer["escalation_required"] and no_answer["escalation_route"] is not None:
            raise ContractValidationError(
                "non-escalating no-answer policy cannot name an escalation route"
            )
    elif record_type == "source_registry":
        source_ids = [item["source_id"] for item in instance["sources"]]
        if len(source_ids) != len(set(source_ids)):
            raise ContractValidationError("source registry source IDs must be unique")
        connector_ids = [item["connector"]["connector_id"] for item in instance["sources"]]
        if len(connector_ids) != len(set(connector_ids)):
            raise ContractValidationError("source registry connector IDs must be unique")
        for item in instance["sources"]:
            approval = item["approval"]
            reviewed = (
                approval["reviewer_identity"] is not None
                and approval["reviewed_at"] is not None
            )
            if approval["status"] == "candidate" and reviewed:
                raise ContractValidationError(
                    "candidate source cannot carry a completed review"
                )
            if approval["status"] != "candidate" and not reviewed:
                raise ContractValidationError(
                    "decided source requires reviewer identity and review time"
                )
            if approval["status"] == "approved":
                if not all(approval["checks"].values()):
                    raise ContractValidationError(
                        "approved source requires every classification check"
                    )
                if item["authority_class"] == "unverified":
                    raise ContractValidationError(
                        "unverified source authority cannot be approved"
                    )
                if item["rights"]["redistribution"] == "prohibited":
                    raise ContractValidationError(
                        "source with prohibited redistribution cannot be approved"
                    )
            connector = item["connector"]
            parsed = urlparse(connector["url"])
            hostname = (parsed.hostname or "").lower()
            allowed_hosts = {host.lower() for host in connector["allowed_hosts"]}
            try:
                port = parsed.port
            except ValueError as exc:
                raise ContractValidationError("source registry connector has an invalid port") from exc
            if parsed.scheme != "https" or not hostname or port not in (None, 443):
                raise ContractValidationError("source registry connectors require HTTPS on port 443")
            if parsed.username is not None or parsed.password is not None or parsed.fragment:
                raise ContractValidationError("source registry URL cannot contain credentials or fragments")
            if any(not label for label in hostname.split(".")):
                raise ContractValidationError("source registry URL hostname is malformed")
            if hostname not in allowed_hosts:
                raise ContractValidationError("source registry URL host is not allowlisted")
    elif record_type == "acquisition_report":
        body = {key: value for key, value in instance.items() if key != "report_id"}
        expected = "sha256:" + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if instance["report_id"] != expected:
            raise ContractValidationError("acquisition report_id does not match report body")
        previous = instance["previous_digest"]
        current = instance["current_digest"]
        expected_change = "initial" if previous is None else ("unchanged" if previous == current else "changed")
        if instance["change"] != expected_change:
            raise ContractValidationError("acquisition change classification does not match digests")
    elif record_type == "refresh_plan":
        body = {key: value for key, value in instance.items() if key != "plan_id"}
        expected = "sha256:" + hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if instance["plan_id"] != expected:
            raise ContractValidationError("refresh plan_id does not match plan body")
        source_ids = [entry["source_id"] for entry in instance["entries"]]
        if source_ids != sorted(source_ids) or len(source_ids) != len(set(source_ids)):
            raise ContractValidationError("refresh plan sources must be unique and sorted")
        expected_due = [entry["source_id"] for entry in instance["entries"] if entry["status"] == "due"]
        if instance["due_source_ids"] != expected_due:
            raise ContractValidationError("refresh due_source_ids do not match entries")


def validate_instance(
    instance: Mapping[str, Any], expected_record_type: Optional[str] = None
) -> None:
    """Validate one already-decoded record, raising ContractValidationError."""

    record_type = instance.get("record_type")
    if expected_record_type is not None and record_type != expected_record_type:
        raise ContractValidationError(
            "expected record_type {0}, got {1}".format(expected_record_type, record_type)
        )
    schema = load_schema(str(record_type))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(instance),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ContractValidationError("{0}: {1}".format(location, error.message))
    _semantic_validate(instance)


def validate_file(
    path: Union[Path, str], expected_record_type: Optional[str] = None
) -> Dict[str, Any]:
    """Load and validate a contract JSON file, returning its decoded object."""

    instance = load_json(path)
    validate_instance(instance, expected_record_type=expected_record_type)
    return instance
