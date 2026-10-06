"""Tests for deterministic portable embedding lineage artifacts."""

from __future__ import annotations

import copy
import json

import pytest

from corpus_factory.embeddings import (
    EmbeddingLineageError,
    read_embedding_jsonl,
    write_embedding_jsonl,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _chunks():
    values = []
    for chunk_id, content, digest in (
        ("chunk-b", "Bravo", "sha256:8123f58e72483f148509ae2da7feda62076dbe2ae3a045323bea4458a62d0952"),
        ("chunk-a", "Alpha", "sha256:b1a96dd646bccaa24cef7a3db22a6f995f05658f4f1c3272913e258c03e6fb24"),
    ):
        values.append({"chunk_id": chunk_id, "content": content, "content_digest": digest})
    return values


def _identity():
    return {
        "weights_digest": _digest("1"),
        "tokenizer_digest": _digest("2"),
        "runtime_digest": _digest("3"),
        "dimension": 3,
        "normalization": "l2",
        "quantization_digest": _digest("4"),
    }


def _embedder(texts):
    values = {
        "Alpha": [1.0, 0.0, 0.0],
        "Bravo": [0.0, 1.0, 0.0],
    }
    return [values[text] for text in texts]


def _rewrite(path, documents):
    path.write_text(
        "".join(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n" for value in documents),
        encoding="utf-8",
    )


def test_exact_rebuild_is_byte_identical_and_round_trips(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first_digest = write_embedding_jsonl(first, _chunks(), _identity(), _embedder)
    second_digest = write_embedding_jsonl(
        second, list(reversed(_chunks())), _identity(), _embedder
    )
    assert first.read_bytes() == second.read_bytes()
    assert first_digest == second_digest
    artifact = read_embedding_jsonl(first, _chunks(), first_digest)
    assert artifact.aggregate_digest == first_digest
    assert [record["chunk_id"] for record in artifact.records] == ["chunk-a", "chunk-b"]


def test_model_identity_change_changes_aggregate_identity(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first_digest = write_embedding_jsonl(first, _chunks(), _identity(), _embedder)
    changed = _identity()
    changed["runtime_digest"] = _digest("a")
    second_digest = write_embedding_jsonl(second, _chunks(), changed, _embedder)
    assert first_digest != second_digest
    assert first.read_bytes() != second.read_bytes()


def test_vector_tampering_is_rejected(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    write_embedding_jsonl(path, _chunks(), _identity(), _embedder)
    documents = [json.loads(line) for line in path.read_text().splitlines()]
    documents[1]["vector"][0] = 0.25
    _rewrite(path, documents)
    with pytest.raises(EmbeddingLineageError, match="vector digest mismatch"):
        read_embedding_jsonl(path, _chunks())


def test_wrong_dimension_and_nonfinite_vectors_are_rejected(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    with pytest.raises(EmbeddingLineageError, match="dimension"):
        write_embedding_jsonl(path, _chunks(), _identity(), lambda texts: [[1.0]] * len(texts))
    with pytest.raises(EmbeddingLineageError, match="non-finite"):
        write_embedding_jsonl(
            path,
            _chunks(),
            _identity(),
            lambda texts: [[float("nan"), 0.0, 0.0]] * len(texts),
        )


def test_declared_l2_normalization_is_enforced(tmp_path):
    with pytest.raises(EmbeddingLineageError, match="claims l2 normalization"):
        write_embedding_jsonl(
            tmp_path / "embeddings.jsonl",
            _chunks(),
            _identity(),
            lambda texts: [[2.0, 0.0, 0.0]] * len(texts),
        )


def test_duplicate_chunks_are_rejected_before_embedding(tmp_path):
    duplicate = _chunks() + [copy.deepcopy(_chunks()[0])]
    with pytest.raises(EmbeddingLineageError, match="duplicate chunk_id"):
        write_embedding_jsonl(tmp_path / "embeddings.jsonl", duplicate, _identity(), _embedder)


def test_noncanonical_order_is_rejected_on_read(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    write_embedding_jsonl(path, _chunks(), _identity(), _embedder)
    documents = [json.loads(line) for line in path.read_text().splitlines()]
    documents[1], documents[2] = documents[2], documents[1]
    _rewrite(path, documents)
    with pytest.raises(EmbeddingLineageError, match="canonical chunk_id order"):
        read_embedding_jsonl(path, _chunks())


def test_content_lineage_mismatch_is_rejected(tmp_path):
    path = tmp_path / "embeddings.jsonl"
    write_embedding_jsonl(path, _chunks(), _identity(), _embedder)
    expected = _chunks()
    expected[0]["content"] = "Changed source chunk"
    with pytest.raises(EmbeddingLineageError, match="content digest mismatch"):
        read_embedding_jsonl(path, expected)
