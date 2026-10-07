"""Disconnected embedding-model packaging and fail-closed startup policy."""

from pathlib import Path

import numpy as np
import pytest

from backend.services.rag_service import embedding_service as module


class _FakeSentenceTransformer:
    calls = []

    def __init__(self, model, **kwargs):
        self.calls.append((str(model), kwargs))

    def encode(self, values, **kwargs):
        return np.zeros((len(values), 384), dtype=float)

    def save(self, path):
        raise AssertionError("a baked model must never be written at runtime")


@pytest.mark.asyncio
async def test_embedding_service_prefers_image_baked_openvino_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_root = tmp_path / "models"
    (model_root / "all-MiniLM-L6-v2_openvino").mkdir(parents=True)
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(model_root))
    monkeypatch.setenv("EMBEDDING_CACHE_DIR", str(cache_root))
    monkeypatch.setattr(module.settings, "embedding_require_local_model", True)
    monkeypatch.setattr(module, "SENTENCE_TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(module, "OPENVINO_AVAILABLE", True)
    monkeypatch.setattr(module, "SentenceTransformer", _FakeSentenceTransformer)
    _FakeSentenceTransformer.calls = []

    service = module.LocalEmbeddingService()

    assert await service.initialize() is True
    assert service.model_source == "baked"
    assert _FakeSentenceTransformer.calls == [
        (str(model_root / "all-MiniLM-L6-v2_openvino"), {"backend": "openvino"})
    ]


@pytest.mark.asyncio
async def test_required_local_model_never_attempts_network_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(tmp_path / "missing-models"))
    monkeypatch.setenv("EMBEDDING_CACHE_DIR", str(tmp_path / "empty-cache"))
    monkeypatch.setattr(module.settings, "embedding_require_local_model", True)
    monkeypatch.setattr(module, "SENTENCE_TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(module, "OPENVINO_AVAILABLE", True)
    monkeypatch.setattr(module, "SentenceTransformer", _FakeSentenceTransformer)
    _FakeSentenceTransformer.calls = []

    service = module.LocalEmbeddingService()

    assert await service.initialize() is False
    assert _FakeSentenceTransformer.calls == []


def test_rag_image_pins_model_revision_and_forces_offline_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("Containerfile.rag", "Dockerfile.rag"):
        contents = (root / "backend" / name).read_text(encoding="utf-8")
        assert "1110a243fdf4706b3f48f1d95db1a4f5529b4d41" in contents
        assert "HF_HUB_OFFLINE=1" in contents
        assert "TRANSFORMERS_OFFLINE=1" in contents
