"""Strict contract tests for event and deployment suitability policy."""

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "corpus_factory" / "schemas" / "event-policy.schema.json"
VALID_PATH = ROOT / "corpus_factory" / "fixtures" / "valid" / "event-policy.json"
INVALID_PATH = (
    ROOT
    / "corpus_factory"
    / "fixtures"
    / "invalid"
    / "event-policy-missing-critical-coverage.json"
)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _validator():
    schema = _load(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _errors(instance):
    return list(_validator().iter_errors(instance))


def test_valid_event_policy_contract():
    assert _errors(_load(VALID_PATH)) == []


def test_missing_critical_coverage_is_invalid():
    errors = _errors(_load(INVALID_PATH))
    assert errors
    assert any(error.validator == "contains" for error in errors)


def test_contract_rejects_unknown_fields_at_nested_boundaries():
    policy = _load(VALID_PATH)
    policy["coverage_requirements"][0]["implicit_override"] = True
    errors = _errors(policy)
    assert any(error.validator == "additionalProperties" for error in errors)


def test_critical_coverage_must_block_when_stale():
    policy = _load(VALID_PATH)
    policy["coverage_requirements"][0]["freshness"]["stale_action"] = "warn"
    errors = _errors(policy)
    assert any(error.validator == "const" for error in errors)


def test_every_coverage_requirement_needs_a_selector():
    policy = _load(VALID_PATH)
    selector = policy["coverage_requirements"][0]["selector"]
    selector["required_fact_ids"] = []
    selector["required_document_ids"] = []
    errors = _errors(policy)
    assert any(error.validator == "anyOf" for error in errors)


def test_evaluation_case_minimums_cannot_be_zero():
    for field in ("minimum_positive", "minimum_boundary", "minimum_no_answer"):
        policy = _load(VALID_PATH)
        policy["coverage_requirements"][0]["evaluation_cases"][field] = 0
        errors = _errors(policy)
        assert any(error.validator == "minimum" for error in errors), field


def test_escalation_requires_a_route():
    policy = copy.deepcopy(_load(VALID_PATH))
    policy["no_answer_policy"]["escalation_route"] = None
    errors = _errors(policy)
    assert errors


def test_authority_entries_require_exact_source_and_publisher_allowlists():
    policy = _load(VALID_PATH)
    del policy["authority_registry"][0]["source_ids"]
    errors = _errors(policy)
    assert any(error.validator == "required" for error in errors)


def test_policy_approvals_require_independence_group():
    policy = _load(VALID_PATH)
    del policy["human_approval_attestations"][0]["independence_group"]
    errors = _errors(policy)
    assert any(error.validator == "required" for error in errors)
