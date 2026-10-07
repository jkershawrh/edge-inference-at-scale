"""TDD for independently approved evaluation and exact-digest signing."""

from __future__ import annotations

import copy
from pathlib import Path

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from corpus_factory.evaluation_attestation import (
    EvaluationAttestationError,
    authorize_release_signing,
    bytes_digest,
    finalize_attestation,
    object_digest,
    prepare_attestation_statement,
    signing_payload,
    verify_attestation,
)
from corpus_factory.validator import validate_instance
from scripts.authorize_corpus_signing import main as authorize_main
from scripts.finalize_evaluation_attestation import main as finalize_main
from scripts.prepare_evaluation_attestation import main as prepare_main


def _report(candidate: bytes):
    binding_names = (
        "corpus_digest",
        "eval_set_digest",
        "model_digest",
        "embedding_model_digest",
        "chunker_digest",
        "policy_digest",
        "source_aggregate_digest",
        "document_aggregate_digest",
        "case_aggregate_digest",
    )
    body = {
        "schema_version": "1.1.0",
        "spec_version": "1.0",
        "contract_profile": "governed-v1",
        "binding": {
            "release_digest": bytes_digest(candidate),
            **{
                name: "sha256:" + format(index, "064x")
                for index, name in enumerate(binding_names, start=1)
            },
        },
        "layers": {
            name: {"passed": True}
            for name in (
                "corpus_suitability",
                "corpus_coverage",
                "release_validity",
                "retrieval",
                "grounded_answer",
                "edge_operation",
            )
        },
        "failures": [],
        "decision": "PASS",
    }
    return {"report_id": object_digest(body), **body}


def _approvals():
    return [
        {
            "identity": "urn:example:person:evaluation-owner",
            "role": "evaluation_owner",
            "organization": "Independent Evaluation Office",
            "independence_group": "evaluation-governance",
            "decision": "approve",
            "approved_at": "2026-10-06T18:05:00Z",
        },
        {
            "identity": "urn:example:person:release-approver",
            "role": "release_approver",
            "organization": "Field Release Authority",
            "independence_group": "release-governance",
            "decision": "approve",
            "approved_at": "2026-10-06T18:10:00Z",
        },
    ]


def _attestation(candidate=b'{"candidate":1}\n'):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    report = _report(candidate)
    statement = prepare_attestation_statement(
        report,
        _approvals(),
        evaluation_executor_identity="urn:example:service:evaluator",
        valid_from="2026-10-06T18:00:00Z",
        expires_at="2026-10-07T18:00:00Z",
        revocation_generation=7,
        revocation_checked_at="2026-10-06T18:09:00Z",
        signer_identity="urn:example:service:attestation-signer",
        signed_at="2026-10-06T18:15:00Z",
        public_key=public_key,
        key_valid_from="2026-10-01T00:00:00Z",
        key_expires_at="2027-01-01T00:00:00Z",
    )
    record = finalize_attestation(
        statement, private_key.sign(signing_payload(statement)), public_key
    )
    return candidate, report, record, private_key, public_key


def test_attestation_binds_exact_report_release_and_independent_approvals():
    candidate, report, record, _, public_key = _attestation()

    validate_instance(record, "evaluation_attestation")
    verify_attestation(record, report, public_key, as_of="2026-10-06T19:00:00Z")
    authorization = authorize_release_signing(
        candidate, report, record, public_key, as_of="2026-10-06T19:00:00Z"
    )

    assert record["subject"]["promotion_report_digest"] == object_digest(report)
    assert record["subject"]["release_digest"] == bytes_digest(candidate)
    assert {item["role"] for item in record["approvals"]} == {
        "evaluation_owner",
        "release_approver",
    }
    assert authorization["candidate_release_digest"] == bytes_digest(candidate)
    assert authorization["release_signature_bindings"] == {
        "evaluation_report_digest": object_digest(report),
        "approval_attestation_digests": [object_digest(record)],
    }
    assert authorization["authorization"] == {
        "action": "sign_exact_candidate_digest",
        "required_key_role": "corpus-release",
        "authorized_at": "2026-10-06T19:00:00Z",
        "private_key_available": False,
    }


@pytest.mark.parametrize("collision", ["executor", "signer", "group"])
def test_separation_of_duties_is_enforced_before_signing(collision):
    private_key = Ed25519PrivateKey.generate()
    approvals = _approvals()
    executor = "urn:example:service:evaluator"
    signer = "urn:example:service:attestation-signer"
    if collision == "executor":
        approvals[0]["identity"] = executor
    elif collision == "signer":
        approvals[1]["identity"] = signer
    else:
        approvals[1]["independence_group"] = approvals[0]["independence_group"]

    with pytest.raises(EvaluationAttestationError, match="independent|distinct|approve"):
        prepare_attestation_statement(
            _report(b"candidate"),
            approvals,
            evaluation_executor_identity=executor,
            valid_from="2026-10-06T18:00:00Z",
            expires_at="2026-10-07T18:00:00Z",
            revocation_generation=1,
            revocation_checked_at="2026-10-06T18:00:00Z",
            signer_identity=signer,
            signed_at="2026-10-06T18:15:00Z",
            public_key=private_key.public_key(),
            key_valid_from="2026-10-01T00:00:00Z",
            key_expires_at="2027-01-01T00:00:00Z",
        )


def test_tampered_report_signature_or_candidate_fail_closed():
    candidate, report, record, _, public_key = _attestation()
    tampered_report = copy.deepcopy(report)
    tampered_report["decision"] = "FAIL"
    with pytest.raises(EvaluationAttestationError, match="report_id"):
        verify_attestation(record, tampered_report, public_key, as_of="2026-10-06T19:00:00Z")

    tampered_record = copy.deepcopy(record)
    tampered_record["subject"]["release_digest"] = "sha256:" + "f" * 64
    with pytest.raises(EvaluationAttestationError, match="attestation_id|signature|subject"):
        verify_attestation(tampered_record, report, public_key, as_of="2026-10-06T19:00:00Z")

    with pytest.raises(EvaluationAttestationError, match="differs"):
        authorize_release_signing(
            candidate + b"tampered",
            report,
            record,
            public_key,
            as_of="2026-10-06T19:00:00Z",
        )


def test_expiration_key_identity_and_external_revocation_fail_closed():
    _, report, record, _, public_key = _attestation()
    with pytest.raises(EvaluationAttestationError, match="predates"):
        verify_attestation(record, report, public_key, as_of="2026-10-06T18:12:00Z")
    with pytest.raises(EvaluationAttestationError, match="not currently valid"):
        verify_attestation(record, report, public_key, as_of="2026-10-08T00:00:00Z")
    with pytest.raises(EvaluationAttestationError, match="revocation state"):
        verify_attestation(
            record,
            report,
            public_key,
            as_of="2026-10-06T19:00:00Z",
            revoked_attestation_ids=[record["attestation_id"]],
        )
    other_key = Ed25519PrivateKey.generate().public_key()
    with pytest.raises(EvaluationAttestationError, match="key ID"):
        verify_attestation(record, report, other_key, as_of="2026-10-06T19:00:00Z")


def test_no_private_key_material_is_present_in_attestation_or_authorization():
    candidate, report, record, _, public_key = _attestation()
    authorization = authorize_release_signing(
        candidate, report, record, public_key, as_of="2026-10-06T19:00:00Z"
    )
    rendered = str(record) + str(authorization)
    assert "PRIVATE KEY" not in rendered
    assert authorization["authorization"]["private_key_available"] is False


def test_cli_round_trip_emits_external_payload_and_authorization(tmp_path):
    candidate, report, _, private_key, public_key = _attestation()
    candidate_path = tmp_path / "manifest.json"
    report_path = tmp_path / "promotion.json"
    approvals_path = tmp_path / "approvals.json"
    public_key_path = tmp_path / "attestation-public.pem"
    statement_path = tmp_path / "statement.json"
    payload_path = tmp_path / "payload.bin"
    signature_path = tmp_path / "signature.bin"
    attestation_path = tmp_path / "attestation.json"
    authorization_path = tmp_path / "authorization.json"
    candidate_path.write_bytes(candidate)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    approvals_path.write_text(json.dumps(_approvals()), encoding="utf-8")
    public_key_path.write_bytes(
        public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    assert prepare_main([
        "--promotion-report", str(report_path),
        "--approvals", str(approvals_path),
        "--evaluation-executor-identity", "urn:example:service:evaluator",
        "--valid-from", "2026-10-06T18:00:00Z",
        "--expires-at", "2026-10-07T18:00:00Z",
        "--revocation-generation", "7",
        "--revocation-checked-at", "2026-10-06T18:09:00Z",
        "--signer-identity", "urn:example:service:attestation-signer",
        "--signed-at", "2026-10-06T18:15:00Z",
        "--public-key", str(public_key_path),
        "--key-valid-from", "2026-10-01T00:00:00Z",
        "--key-expires-at", "2027-01-01T00:00:00Z",
        "--output", str(statement_path),
        "--payload-output", str(payload_path),
    ]) == 0
    signature_path.write_bytes(private_key.sign(payload_path.read_bytes()))
    assert finalize_main([
        "--statement", str(statement_path),
        "--signature", str(signature_path),
        "--public-key", str(public_key_path),
        "--output", str(attestation_path),
    ]) == 0
    assert authorize_main([
        "--candidate-manifest", str(candidate_path),
        "--promotion-report", str(report_path),
        "--evaluation-attestation", str(attestation_path),
        "--attestation-public-key", str(public_key_path),
        "--as-of", "2026-10-06T19:00:00Z",
        "--output", str(authorization_path),
    ]) == 0

    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    assert authorization["candidate_release_digest"] == bytes_digest(candidate)
    assert authorization["authorization"]["required_key_role"] == "corpus-release"
