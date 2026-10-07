"""Tests for retrieval-stage timing instrumentation."""

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("chromadb")

from backend.services.rag_service.main import RAGService
from backend.shared.models import RAGQuery, RAGResult


def _service_without_database() -> RAGService:
    service = RAGService.__new__(RAGService)
    service.collection = object()
    service.stats = {"chromadb_available": True}
    service.timing_totals_ms = {}
    service.timing_counts = {}
    return service


@pytest.mark.asyncio
async def test_hybrid_search_times_each_retrieval_stage():
    service = _service_without_database()
    expected = RAGResult(documents=["shelter"], scores=[0.9], metadata=[{}])
    service._vector_search = AsyncMock(return_value=[])
    service._text_search = AsyncMock(return_value=[])
    service._combine_search_results = AsyncMock(return_value=expected)

    result = await service._hybrid_search(RAGQuery(query="nearest shelter"))
    timings = service.timing_snapshot()

    assert result == expected
    assert timings["vector_search"]["samples"] == 1
    assert timings["text_search"]["samples"] == 1
    assert timings["fusion"]["samples"] == 1


@pytest.mark.asyncio
async def test_total_search_timing_records_failures():
    service = _service_without_database()
    service.stats.update(
        {"total_searches": 0, "successful_searches": 0, "failed_searches": 0}
    )
    service._hybrid_search = AsyncMock(side_effect=RuntimeError("offline"))

    result = await service.search(RAGQuery(query="nearest shelter"))

    assert result.documents == []
    assert service.stats["failed_searches"] == 1
    assert service.timing_snapshot()["search_total"]["samples"] == 1
