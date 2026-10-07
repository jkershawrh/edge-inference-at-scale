"""Pure retrieval helpers shared by the RAG service and unit tests."""

import hashlib
from typing import Any, Dict, List, Tuple


def candidate_pool_size(
    top_k: int, multiplier: int = 4, minimum: int = 10, maximum: int = 50
) -> int:
    """Return a bounded candidate count for rank fusion.

    Retrieving only ``top_k`` items from each source makes fusion unable to
    rescue evidence that one source ranks just outside the response window.
    """
    return min(max(int(top_k) * max(1, int(multiplier)), int(minimum)), int(maximum))


def cosine_distance_to_confidence(distance: float) -> float:
    """Convert Chroma cosine distance (0 is best) to bounded similarity."""
    return max(0.0, min(1.0, 1.0 - float(distance)))


def _result_key(result: Dict[str, Any]) -> str:
    metadata = result.get("metadata") or {}
    parent_id = metadata.get("parent_doc_id") or metadata.get("doc_id")
    if parent_id:
        return str(parent_id)
    document = str(result.get("document", ""))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def fuse_ranked_results(
    vector_results: List[Dict[str, Any]],
    text_results: List[Dict[str, Any]],
    top_k: int,
    min_confidence: float,
    rrf_k: int = 60,
) -> List[Dict[str, Any]]:
    """Fuse independently ranked result lists using reciprocal rank fusion.

    Ranking is based only on each result's position, so unrelated vector-distance
    and lexical-score scales cannot dominate one another. The returned confidence
    remains an evidence-strength signal for existing response-safety thresholds.
    """
    ranked_sources: List[Tuple[str, List[Dict[str, Any]], float]] = []
    if vector_results:
        ranked_sources.append(("vector", vector_results, 0.5))
    if text_results:
        ranked_sources.append(("text", text_results, 0.5))
    if not ranked_sources:
        return []

    active_weight = sum(source[2] for source in ranked_sources)
    best_possible_rrf = active_weight / float(rrf_k + 1)
    combined: Dict[str, Dict[str, Any]] = {}

    for source_name, results, weight in ranked_sources:
        for rank, result in enumerate(results, start=1):
            key = _result_key(result)
            entry = combined.setdefault(
                key,
                {
                    "document": result.get("document", ""),
                    "metadata": dict(result.get("metadata") or {}),
                    "rrf_raw": 0.0,
                    "source_scores": {},
                    "source_ranks": {},
                },
            )
            # Prefer the vector chunk as LLM context when both sources describe
            # the same parent document; it is smaller and semantically focused.
            if source_name == "vector":
                entry["document"] = result.get("document", entry["document"])
                entry["metadata"].update(result.get("metadata") or {})
            entry["rrf_raw"] += weight / float(rrf_k + rank)
            entry["source_scores"][source_name] = float(result.get("score", 0.0))
            entry["source_ranks"][source_name] = rank

    fused: List[Dict[str, Any]] = []
    for entry in combined.values():
        sources = sorted(entry["source_scores"].keys())
        confidence = max(entry["source_scores"].values(), default=0.0)
        if len(sources) > 1:
            confidence = min(1.0, confidence + 0.1)
        if confidence < min_confidence:
            continue

        fused_score = entry["rrf_raw"] / best_possible_rrf
        metadata = entry["metadata"]
        metadata.update(
            {
                "retrieval_sources": sources,
                "retrieval_source_ranks": entry["source_ranks"],
                "retrieval_source_scores": entry["source_scores"],
                "retrieval_fused_score": round(fused_score, 6),
                "retrieval_confidence": round(confidence, 6),
            }
        )
        fused.append(
            {
                "document": entry["document"],
                "metadata": metadata,
                "score": confidence,
                "fused_score": fused_score,
            }
        )

    # Exact lexical rank is the deterministic tie-breaker for equal fusion
    # scores. At the edge, returning the source passage is safer than allowing
    # a semantically nearby document to win solely because it was encountered
    # first in the vector result list.
    fused.sort(
        key=lambda item: (
            -item["fused_score"],
            -item["score"],
            item["metadata"].get("retrieval_source_ranks", {}).get("text", 10**9),
            item["metadata"].get("parent_doc_id", ""),
        )
    )
    return fused[:top_k]
