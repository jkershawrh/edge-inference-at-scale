"""Conflict and reviewer separation-of-duties policy tests."""

import copy
import hashlib
import json

import pytest

from corpus_factory.review import ReviewPolicy, ReviewPolicyError, evaluate_review_policy


def _digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _citation(source_id="source-official"):
    return {
        "source_id": source_id,
        "evidence_digest": _digest(source_id),
        "location": "unicode_code_point:0-10",
    }


def _document(document_id, value, *, geography="north", safety="critical", rank=50):
    citation = _citation()
    text = "Shelter status: {0}".format(value)
    return {
        "schema_version": "2.0.0",
        "record_type": "canonical_document",
        "document_id": document_id,
        "revision": 1,
        "document_digest": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "event_id": "storm-2026",
        "deployment_ids": ["deployment-a"],
        "scope": {
            "geographies": [geography],
            "languages": ["en"],
            "audiences": ["public"],
        },
        "canonical_text": text,
        "structured_facts": [
            {"fact_id": "shelter-status", "value": value, "citations": [citation]}
        ],
        "source_citations": [citation],
        "transformations": [{"tool": "test", "version": "1", "operation": "fixture"}],
        "validity": {
            "valid_from": "2026-10-06T00:00:00Z",
            "valid_until": "2026-10-08T00:00:00Z",
            "review_due_at": "2026-10-07T00:00:00Z",
            "stale_action": "block" if safety == "critical" else "warn",
        },
        "authority_rank": rank,
        "conflict": {"state": "none", "explanation": "", "supersedes": []},
        "safety_class": safety,
        "permitted_delivery_channels": ["sms"],
        "review_attestation_ids": ["review-{0}".format(document_id)],
        "approval_state": "approved",
    }


def _attestation(document, role, suffix, *, group=None):
    return {
        "schema_version": "2.0.0",
        "record_type": "review_attestation",
        "attestation_id": "att-{0}-{1}".format(document["document_id"], suffix),
        "subject": {
            "type": "document",
            "id": document["document_id"],
            "digest": document["document_digest"],
        },
        "reviewer": {
            "identity": "reviewer-{0}@example.test".format(suffix),
            "role": role,
            "organization": "Relief Organization",
        },
        "decision": "approve",
        "checklist": {
            "id": "critical-review",
            "version": "1",
            "responses_digest": _digest(suffix),
        },
        "reviewed_at": "2026-10-06T12:00:00Z",
        "comments": "Reviewed",
        "independence_group": group or "group-{0}".format(suffix),
        "critical_approvals_required": 2,
        "local_validation": {
            "required": True,
            "completed": True,
            "language": "en",
            "community": "north",
        },
    }


def _reviews(*documents):
    records = []
    for index, document in enumerate(documents):
        document_reviews = [
            _attestation(document, "domain_sme", "{0}-domain".format(index)),
            _attestation(document, "local_sme", "{0}-local".format(index)),
        ]
        document["review_attestation_ids"] = [
            item["attestation_id"] for item in document_reviews
        ]
        records.extend(document_reviews)
    return records


def test_different_geographies_do_not_conflict():
    north = _document("doc-north", "open", geography="north")
    south = _document("doc-south", "closed", geography="south")
    outcome = evaluate_review_policy([south, north], _reviews(north, south))

    assert outcome.conflicts == ()
    assert not outcome.approval_blocked
    assert all(item["conflict"]["state"] == "none" for item in outcome.documents)


def test_overlapping_critical_contradiction_blocks_approval():
    first = _document("doc-first", "open")
    second = _document("doc-second", "closed")
    outcome = evaluate_review_policy([first, second], _reviews(first, second))

    assert outcome.approval_blocked
    assert outcome.blocked_document_ids == ("doc-first", "doc-second")
    assert outcome.conflicts[0].state == "contested"
    assert all(item["approval_state"] == "in_review" for item in outcome.documents)


def test_explicit_authority_policy_and_supersession_resolve_conflict():
    older = _document("doc-older", "closed", rank=30)
    authoritative = _document("doc-authoritative", "open", rank=90)
    authoritative["conflict"]["supersedes"] = [
        {
            "document_id": older["document_id"],
            "revision": older["revision"],
            "digest": older["document_digest"],
        }
    ]
    outcome = evaluate_review_policy(
        [older, authoritative],
        _reviews(older, authoritative),
        ReviewPolicy(allow_authority_resolution=True, minimum_authority_gap=20),
    )

    assert not outcome.approval_blocked
    assert outcome.conflicts[0].state == "resolved"
    assert outcome.conflicts[0].winner_document_id == "doc-authoritative"
    assert all(item["conflict"]["state"] == "resolved" for item in outcome.documents)


def test_authority_policy_does_not_infer_supersession():
    older = _document("doc-older", "closed", rank=30)
    authoritative = _document("doc-authoritative", "open", rank=90)
    outcome = evaluate_review_policy(
        [older, authoritative],
        _reviews(older, authoritative),
        ReviewPolicy(allow_authority_resolution=True, minimum_authority_gap=20),
    )

    assert outcome.approval_blocked
    assert outcome.conflicts[0].state == "contested"
    assert outcome.conflicts[0].winner_document_id is None


def test_noncritical_contested_facts_remain_visible():
    first = _document("doc-one", "open", safety="standard")
    second = _document("doc-two", "closed", safety="standard")
    original_facts = [copy.deepcopy(first["structured_facts"]), copy.deepcopy(second["structured_facts"])]
    outcome = evaluate_review_policy([first, second], [])

    assert not outcome.approval_blocked
    assert [item["structured_facts"] for item in outcome.documents] == original_facts
    assert all(item["conflict"]["state"] == "contested" for item in outcome.documents)


def test_critical_document_rejects_insufficient_reviewer_roles():
    document = _document("doc-critical", "open")
    attestation = _attestation(document, "domain_sme", "only")
    document["review_attestation_ids"] = [attestation["attestation_id"]]
    with pytest.raises(ReviewPolicyError, match="missing independent roles"):
        evaluate_review_policy([document], [attestation])


def test_critical_document_rejects_duplicate_reviewer_roles():
    document = _document("doc-critical", "open")
    attestations = [
        _attestation(document, "domain_sme", "first"),
        _attestation(document, "domain_sme", "second"),
    ]
    document["review_attestation_ids"] = [
        item["attestation_id"] for item in attestations
    ]
    with pytest.raises(ReviewPolicyError, match="duplicate reviewer roles"):
        evaluate_review_policy(
            [document],
            attestations,
            ReviewPolicy(required_critical_roles=("domain_sme",)),
        )


def test_unreferenced_approval_cannot_satisfy_critical_review():
    document = _document("doc-critical", "open")
    referenced = _attestation(document, "domain_sme", "referenced")
    unreferenced = _attestation(document, "local_sme", "unreferenced")
    document["review_attestation_ids"] = [referenced["attestation_id"]]

    with pytest.raises(ReviewPolicyError, match="missing independent roles"):
        evaluate_review_policy([document], [referenced, unreferenced])
