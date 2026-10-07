"""End-to-end suitability drill using the finalized production contracts."""
import hashlib
import json
from pathlib import Path

from corpus_factory.suitability import CASE_FIELDS, CASE_FAMILIES, evaluate_suitability
from corpus_factory.validator import event_policy_subject_digest, validate_instance


FIXTURES = Path(__file__).resolve().parents[2] / "corpus_factory" / "examples" / "synthetic_disaster"
AS_OF = "2026-10-06T18:00:00Z"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_drill_inventory():
    """Load and contract-validate the reusable production-shaped inventory."""
    policy = _load("policy.json")
    sources = _load("sources.json")
    documents = _load("canonical_documents.json")
    cases = _load("evaluation_cases.json")
    validate_instance(policy, "event_policy")
    for source in sources:
        validate_instance(source, "source_record")
    for document in documents:
        validate_instance(document, "canonical_document")
    assert all(set(case) == CASE_FIELDS and case["family"] in CASE_FAMILIES for case in cases)
    return policy, sources, documents, cases


def select_inventory(records, identity_field, selected_ids):
    """Select a deterministic candidate subset from the shared fixture inventory."""
    wanted = set(selected_ids)
    return [record for record in records if record[identity_field] in wanted]


def evaluate(policy, sources, documents, cases):
    return evaluate_suitability(
        policy,
        sources,
        documents,
        cases,
        as_of=AS_OF,
        time_confidence="trusted",
    )


def test_fixture_contracts_evidence_and_scenario_boundaries_are_exact():
    policy, sources, documents, cases = load_drill_inventory()
    source_by_id = {source["source_id"]: source for source in sources}
    document_by_id = {document["document_id"]: document for document in documents}

    assert event_policy_subject_digest(policy) == policy["human_approval_attestations"][0]["policy_digest"]
    assert {entry["publisher_ids"][0] for entry in policy["authority_registry"]} == {
        "publisher-r4-emergency", "publisher-r4-water", "publisher-r5-emergency"
    }
    for source in sources:
        evidence_path = FIXTURES / source["locator"]["reference"]
        payload = evidence_path.read_bytes()
        assert source["evidence"]["byte_size"] == len(payload)
        assert source["evidence"]["digest"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    for document in documents:
        assert document["document_digest"] == "sha256:" + hashlib.sha256(
            document["canonical_text"].encode("utf-8")
        ).hexdigest()
        for citation in document["source_citations"]:
            assert citation["evidence_digest"] == source_by_id[citation["source_id"]]["evidence"]["digest"]

    correction = document_by_id["doc-evacuation-r4-current"]
    expired = document_by_id["doc-evacuation-r4-old"]
    assert correction["conflict"]["supersedes"] == [{
        "document_id": expired["document_id"],
        "revision": expired["revision"],
        "digest": expired["document_digest"],
    }]
    assert expired["validity"]["valid_until"] < AS_OF
    assert document_by_id["doc-shelter-r5-distractor"]["scope"]["geographies"] == [
        "region-5/zone-north"
    ]
    case_ids = {case["case_id"] for case in cases}
    assert any("neighbor" in case_id for case_id in case_ids)
    assert any("expired-route" in case_id for case_id in case_ids)
    assert any("insulin" in case_id for case_id in case_ids)
    assert {case["language"] for case in cases} == {"en", "es"}
    assert {case["channel"] for case in cases} == {"sms", "lora"}


def test_incomplete_candidate_fails_before_corrected_inventory_passes():
    policy, sources, documents, cases = load_drill_inventory()
    incomplete_sources = select_inventory(
        sources,
        "source_id",
        {"source-shelter-r4", "source-water-r4", "source-evacuation-old"},
    )
    incomplete_documents = select_inventory(
        documents,
        "document_id",
        {"doc-shelter-r4-en", "doc-water-r4-es", "doc-evacuation-r4-old"},
    )
    incomplete_cases = [
        case for case in cases if case["channel"] == "sms" and case["language"] == "en"
    ]

    incomplete = evaluate(policy, incomplete_sources, incomplete_documents, incomplete_cases)
    corrected = evaluate(policy, sources, documents, cases)

    assert incomplete["decision"] == "FAIL"
    incomplete_codes = {failure["code"] for failure in incomplete["failures"]}
    assert "MISSING_REQUIRED_DOCUMENT" in incomplete_codes
    assert "NO_QUALIFYING_DOCUMENT" in incomplete_codes
    assert "EVALUATION_COVERAGE_GAP" in incomplete_codes
    assert corrected["decision"] == "PASS"
    assert corrected["failures"] == []
    assert all(requirement["passed"] for requirement in corrected["requirements"])
