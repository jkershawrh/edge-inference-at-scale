"""Operational suitability tests against the finalized event-policy contract."""

import copy
import hashlib
import json
from pathlib import Path

from corpus_factory.suitability import evaluate_suitability
from corpus_factory.validator import event_policy_subject_digest


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "corpus_factory" / "fixtures" / "valid"
AS_OF = "2026-10-06T12:30:00Z"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _source(source_id, publisher_id, authority_class, subject, digest_character):
    source = _fixture("source-record.json")
    source.update(
        source_id=source_id,
        publisher={"id": publisher_id, "name": publisher_id},
        authority_class=authority_class,
        acquired_at="2026-10-06T11:00:00Z",
        last_verified_at="2026-10-06T12:00:00Z",
    )
    source["evidence"]["digest"] = "sha256:" + digest_character * 64
    source["scope"] = {
        "geographies": ["region-north"], "languages": ["en"],
        "audiences": ["public"], "subjects": [subject],
    }
    source["freshness"] = {
        "effective_from": "2026-10-06T11:00:00Z",
        "valid_until": "2026-10-07T12:00:00Z",
        "expected_refresh_seconds": 1800,
        "stale_action": "block" if authority_class == "official" else "warn",
    }
    return source


def _citation(source, location):
    return {"source_id": source["source_id"], "evidence_digest": source["evidence"]["digest"], "location": location}


def _document(document_id, safety_class, fact_id, sources, channels):
    document = _fixture("canonical-document.json")
    document.update(
        document_id=document_id,
        event_id="field-event-2026",
        deployment_ids=["site-north"],
        scope={"geographies": ["region-north"], "languages": ["en"], "audiences": ["public"]},
        safety_class=safety_class,
        permitted_delivery_channels=channels,
        approval_state="approved",
    )
    document["document_digest"] = "sha256:" + hashlib.sha256(
        document["canonical_text"].encode("utf-8")
    ).hexdigest()
    citations = [_citation(source, f"$.evidence[{index}]") for index, source in enumerate(sources)]
    document["source_citations"] = copy.deepcopy(citations)
    document["structured_facts"] = [{"fact_id": fact_id, "value": "open", "citations": copy.deepcopy(citations)}]
    document["validity"] = {
        "valid_from": "2026-10-06T11:00:00Z", "valid_until": "2026-10-07T12:00:00Z",
        "review_due_at": "2026-10-06T18:00:00Z",
        "stale_action": "block" if safety_class == "critical" else "warn",
    }
    document["conflict"] = {"state": "none", "explanation": "", "supersedes": []}
    return document


def _inventory():
    primary = _source("source-shelter-primary", "publisher-public-safety", "official", "shelter-status", "1")
    secondary = _source("source-shelter-secondary", "publisher-public-safety", "official", "shelter-status", "2")
    local = _source("source-local-context", "publisher-local-review", "community", "local-context", "3")
    documents = [
        _document("doc-critical-status", "critical", "shelter-status", [primary, secondary], ["sms", "lora"]),
        _document("doc-local-context", "advisory", "local-context", [local], ["sms"]),
    ]
    return [primary, secondary, local], documents


def _cases():
    cases = []
    sequence = 0
    for channel in ("sms", "lora"):
        for family, count in (("positive", 3), ("boundary", 2), ("no_answer", 2)):
            for _ in range(count):
                sequence += 1
                cases.append({
                    "case_id": f"critical-{sequence:03d}", "requirement_id": "coverage-critical-status",
                    "family": family, "geography": "region-north", "language": "en",
                    "audience": "public", "channel": channel, "approved": True,
                })
    for family in ("positive", "boundary", "no_answer"):
        sequence += 1
        cases.append({
            "case_id": f"advisory-{sequence:03d}", "requirement_id": "coverage-advisory-context",
            "family": family, "geography": "region-north", "language": "en",
            "audience": "public", "channel": "sms", "approved": True,
        })
    return cases


def _evaluate(policy=None, sources=None, documents=None, cases=None, **kwargs):
    default_sources, default_documents = _inventory()
    return evaluate_suitability(
        policy or _fixture("event-policy.json"),
        sources if sources is not None else default_sources,
        documents if documents is not None else default_documents,
        cases if cases is not None else _cases(),
        as_of=kwargs.get("as_of", AS_OF),
        time_confidence=kwargs.get("time_confidence", "trusted"),
    )


def test_complete_inventory_passes_and_output_is_deterministic():
    sources, documents = _inventory()
    first = _evaluate(sources=sources, documents=documents)
    second = _evaluate(sources=list(reversed(sources)), documents=list(reversed(documents)), cases=list(reversed(_cases())))
    assert first == second
    assert first["decision"] == "PASS"
    assert all(item["passed"] for item in first["requirements"])
    assert first["bindings"]["event_policy_subject_digest"].startswith("sha256:")
    assert first["bindings"]["event_policy_record_digest"].startswith("sha256:")


def test_missing_critical_fact_cannot_be_hidden_by_document_count():
    sources, documents = _inventory()
    documents[0]["structured_facts"] = []
    report = _evaluate(sources=sources, documents=documents)
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "MISSING_REQUIRED_FACT" for item in report["failures"])


def test_wrong_geography_and_language_leave_explicit_scope_gap():
    sources, documents = _inventory()
    documents[0]["scope"].update(geographies=["region-south"], languages=["es"])
    report = _evaluate(sources=sources, documents=documents)
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "SCOPE_COVERAGE_GAP" for item in report["failures"])


def test_unauthorized_source_does_not_satisfy_requirement():
    sources, documents = _inventory()
    sources[0]["publisher"]["id"] = "publisher-not-authorized"
    report = _evaluate(sources=sources, documents=documents)
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "UNREGISTERED_SOURCE" for item in report["failures"])


def test_broken_source_evidence_digest_fails_lineage():
    sources, documents = _inventory()
    bad_digest = "sha256:" + "f" * 64
    documents[0]["source_citations"][0]["evidence_digest"] = bad_digest
    documents[0]["structured_facts"][0]["citations"][0]["evidence_digest"] = bad_digest
    report = _evaluate(sources=sources, documents=documents)
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "BROKEN_EVIDENCE_LINEAGE" for item in report["failures"])


def test_expired_critical_document_cannot_satisfy_requirement():
    sources, documents = _inventory()
    documents[0]["validity"]["valid_until"] = "2026-10-06T12:00:00Z"
    report = _evaluate(sources=sources, documents=documents)
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "NO_QUALIFYING_DOCUMENT" for item in report["failures"])


def test_source_older_than_requirement_maximum_age_is_rejected():
    sources, documents = _inventory()
    sources[0]["last_verified_at"] = "2026-10-06T11:00:00Z"
    report = _evaluate(sources=sources, documents=documents)
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "SOURCE_TOO_OLD" for item in report["failures"])


def test_requirement_minimum_approved_document_count_is_enforced():
    policy = _fixture("event-policy.json")
    policy["coverage_requirements"][0]["minimum_approved_documents"] = 2
    subject_digest = event_policy_subject_digest(policy)
    for approval in policy["human_approval_attestations"]:
        approval["policy_digest"] = subject_digest
    report = _evaluate(policy=policy)
    assert report["decision"] == "FAIL"
    assert any(
        item["code"] == "INSUFFICIENT_APPROVED_DOCUMENTS"
        for item in report["failures"]
    )


def test_missing_no_answer_case_blocks_its_scope_intersection():
    cases = [case for case in _cases() if not (
        case["requirement_id"] == "coverage-critical-status"
        and case["channel"] == "sms" and case["family"] == "no_answer"
    )]
    report = _evaluate(cases=cases)
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "EVALUATION_COVERAGE_GAP" and item["path"].endswith(".no_answer") for item in report["failures"])


def test_unknown_time_blocks_critical_requirement():
    report = _evaluate(time_confidence="unknown")
    assert report["decision"] == "FAIL"
    assert any(item["code"] == "UNTRUSTED_TIME" for item in report["failures"])
