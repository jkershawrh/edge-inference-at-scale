"""Unit tests for dependency-free RAG retrieval helpers."""

from backend.services.rag_service.retrieval import (
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


def test_cosine_distance_converts_to_bounded_confidence():
    assert cosine_distance_to_confidence(0.0) == 1.0
    assert cosine_distance_to_confidence(0.25) == 0.75
    assert cosine_distance_to_confidence(1.5) == 0.0
    assert cosine_distance_to_confidence(-0.2) == 1.0
