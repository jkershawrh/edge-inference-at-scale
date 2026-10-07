"""Unit tests for dependency-free RAG retrieval helpers."""

import pytest

from backend.services.rag_service.document_manager import DocumentManager
from backend.services.rag_service.retrieval import (
    candidate_pool_size,
    cosine_distance_to_confidence,
    fuse_ranked_results,
)


def _result(doc_id, score, source, text=None):
    return {
        "document": text or "document {0}".format(doc_id),
        "metadata": {"parent_doc_id": doc_id},
        "score": score,
        "source": source,
    }


def test_rank_fusion_is_independent_of_source_score_scale():
    vector = [_result("shared", 0.51, "vector"), _result("vector-only", 0.99, "vector")]
    text = [_result("shared", 0.02, "text"), _result("text-only", 1.0, "text")]

    first = fuse_ranked_results(vector, text, top_k=3, min_confidence=0.0)

    vector[0]["score"] = 0.0001
    vector[1]["score"] = 1000000.0
    text[0]["score"] = 999.0
    text[1]["score"] = 0.00001
    second = fuse_ranked_results(vector, text, top_k=3, min_confidence=0.0)

    assert first[0]["metadata"]["parent_doc_id"] == "shared"
    assert second[0]["metadata"]["parent_doc_id"] == "shared"


def test_rank_fusion_merges_parent_document_and_prefers_vector_chunk():
    vector = [_result("doc-1", 0.7, "vector", text="focused chunk")]
    text = [_result("doc-1", 0.8, "text", text="full parent document")]

    results = fuse_ranked_results(vector, text, top_k=3, min_confidence=0.5)

    assert len(results) == 1
    assert results[0]["document"] == "focused chunk"
    assert results[0]["score"] == 0.9
    assert results[0]["metadata"]["retrieval_sources"] == ["text", "vector"]
    assert results[0]["metadata"]["retrieval_fused_score"] == 1.0


def test_rank_fusion_filters_weak_evidence_and_respects_top_k():
    vector = [
        _result("first", 0.9, "vector"),
        _result("second", 0.8, "vector"),
        _result("weak", 0.2, "vector"),
    ]

    results = fuse_ranked_results(vector, [], top_k=1, min_confidence=0.5)

    assert [item["metadata"]["parent_doc_id"] for item in results] == ["first"]


def test_equal_fusion_score_prefers_better_lexical_rank():
    vector = [
        _result("semantic-first", 0.9, "vector"),
        _result("exact-first", 0.8, "vector"),
    ]
    text = [
        _result("exact-first", 0.9, "text"),
        _result("semantic-first", 0.8, "text"),
    ]

    results = fuse_ranked_results(vector, text, top_k=2, min_confidence=0.0)

    assert [item["metadata"]["parent_doc_id"] for item in results] == [
        "exact-first",
        "semantic-first",
    ]


def test_cosine_distance_converts_to_bounded_confidence():
    assert cosine_distance_to_confidence(0.0) == 1.0
    assert cosine_distance_to_confidence(0.25) == 0.75
    assert cosine_distance_to_confidence(1.5) == 0.0
    assert cosine_distance_to_confidence(-0.2) == 1.0


def test_candidate_pool_is_larger_than_response_window_and_bounded():
    assert candidate_pool_size(3) == 12
    assert candidate_pool_size(1) == 10
    assert candidate_pool_size(100) == 50


@pytest.mark.asyncio
async def test_lexical_search_normalizes_questions_and_expands_common_intents(tmp_path):
    manager = DocumentManager(str(tmp_path))
    await manager.add_document(
        "Main Cafeteria is on Level 1.", doc_id="dining", category="venue"
    )
    await manager.add_document(
        "Free wireless network SummitConnect-Guest has no password.",
        doc_id="wifi",
        category="venue",
    )
    await manager.add_document(
        "Shuttle, parking, bus, and rideshare directions to the venue.",
        doc_id="transport",
        category="transport",
    )

    dining = await manager.search_documents("Where can I eat?", limit=3)
    wifi = await manager.search_documents("Is there WiFi?", limit=3)
    transport = await manager.search_documents("How do I get to the venue?", limit=3)

    assert dining[0]["document"]["id"] == "dining"
    assert wifi[0]["document"]["id"] == "wifi"
    assert transport[0]["document"]["id"] == "transport"


@pytest.mark.asyncio
async def test_explicit_category_word_outranks_incidental_mention(tmp_path):
    manager = DocumentManager(str(tmp_path))
    await manager.add_document(
        "Speaker bio. Speaking at four sessions about edge computing.",
        doc_id="speaker",
        category="speakers",
    )
    await manager.add_document(
        "Session: Edge Inference at Scale for edge computing.",
        doc_id="session",
        category="sessions",
    )

    results = await manager.search_documents(
        "What sessions are about edge computing?", limit=2
    )

    assert results[0]["document"]["id"] == "session"
