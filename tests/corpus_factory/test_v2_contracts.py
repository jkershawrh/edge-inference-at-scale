"""Golden-fixture tests for the version 2 corpus release contract."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from corpus_factory.validator import (
    ContractValidationError,
    SCHEMA_BY_RECORD_TYPE,
    load_schema,
    validate_file,
    validate_instance,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "corpus_factory" / "fixtures"

VALID_FIXTURES = {
    "source_record": "source-record.json",
    "canonical_document": "canonical-document.json",
    "chunk_record": "chunk-record.json",
    "review_attestation": "review-attestation.json",
    "release_manifest": "release-manifest.json",
    "activation_receipt": "activation-receipt.json",
    "event_policy": "event-policy.json",
    "source_registry": "source-registry.json",
    "acquisition_report": "acquisition-report.json",
    "refresh_plan": "refresh-plan.json",
}

INVALID_FIXTURES = {
    "source_record": "source-record-missing-evidence-digest.json",
    "canonical_document": "canonical-document-approved-contested.json",
    "chunk_record": "chunk-record-untraceable-parent.json",
    "review_attestation": "review-attestation-unknown-decision.json",
    "release_manifest": "release-manifest-no-sequence.json",
    "activation_receipt": "activation-receipt-contains-user-data.json",
    "event_policy": "event-policy-missing-critical-coverage.json",
    "source_registry": "source-registry-host-not-allowlisted.json",
    "acquisition_report": "acquisition-report-wrong-change.json",
    "refresh_plan": "refresh-plan-mismatched-due-list.json",
}


@pytest.mark.parametrize("record_type", sorted(VALID_FIXTURES))
def test_valid_golden_fixture(record_type):
    value = validate_file(FIXTURES / "valid" / VALID_FIXTURES[record_type], record_type)
    assert value["schema_version"] == "2.0.0"


@pytest.mark.parametrize("record_type", sorted(INVALID_FIXTURES))
def test_invalid_golden_fixture(record_type):
    with pytest.raises(ContractValidationError):
        validate_file(FIXTURES / "invalid" / INVALID_FIXTURES[record_type], record_type)


@pytest.mark.parametrize("record_type", sorted(SCHEMA_BY_RECORD_TYPE))
def test_schema_is_valid_draft_2020_12(record_type):
    schema = load_schema(record_type)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def _valid(record_type):
    path = FIXTURES / "valid" / VALID_FIXTURES[record_type]
    return json.loads(path.read_text(encoding="utf-8"))


def test_unknown_top_level_fields_fail_closed():
    record = _valid("source_record")
    record["future_trust_override"] = True
    with pytest.raises(ContractValidationError, match="Additional properties"):
        validate_instance(record)


def test_duplicate_json_keys_are_rejected(tmp_path):
    ambiguous = tmp_path / "ambiguous.json"
    ambiguous.write_text(
        '{"record_type":"source_record","record_type":"release_manifest"}',
        encoding="utf-8",
    )
    with pytest.raises(ContractValidationError, match="duplicate JSON key"):
        validate_file(ambiguous)


def test_chunk_content_digest_is_verified():
    record = _valid("chunk_record")
    record["content"] = "Unsupported changed content"
    with pytest.raises(ContractValidationError, match="content_digest"):
        validate_instance(record)


def test_release_embedding_metadata_must_match_count():
    record = _valid("release_manifest")
    record["counts"]["embeddings"] = 0
    with pytest.raises(ContractValidationError, match="embedding metadata and count disagree"):
        validate_instance(record)


def test_success_receipt_must_match_desired_release():
    record = _valid("activation_receipt")
    record["activated"]["sequence"] = 6
    with pytest.raises(ContractValidationError, match="desired release"):
        validate_instance(record)


def test_required_local_validation_cannot_be_incomplete():
    record = _valid("review_attestation")
    record["local_validation"]["completed"] = False
    with pytest.raises(ContractValidationError, match="local validation"):
        validate_instance(record)


@pytest.mark.parametrize("safety_class", ["advisory", "standard", "high", "critical"])
def test_canonical_document_uses_evaluation_safety_taxonomy(safety_class):
    record = _valid("canonical_document")
    record["safety_class"] = safety_class
    if safety_class != "critical":
        record["validity"]["stale_action"] = "warn"
    validate_instance(record)


def test_legacy_safety_class_is_rejected():
    record = _valid("canonical_document")
    record["safety_class"] = "important"
    with pytest.raises(ContractValidationError, match="safety_class"):
        validate_instance(record)


def test_event_policy_approval_digest_binds_policy_body():
    record = _valid("event_policy")
    record["mission"] = "Mutated after approval"
    with pytest.raises(ContractValidationError, match="approval digest"):
        validate_instance(record)


def test_event_policy_requirement_cannot_exceed_deployment_scope():
    record = _valid("event_policy")
    record["coverage_requirements"][0]["scope"]["languages"].append("es")
    with pytest.raises(ContractValidationError, match="exceeds deployment scope"):
        validate_instance(record)


def test_validator_does_not_mutate_input():
    record = _valid("canonical_document")
    original = copy.deepcopy(record)
    validate_instance(record)
    assert record == original
