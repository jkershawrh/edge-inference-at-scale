"""Portable, deterministic embedding artifacts with end-to-end lineage.

This module deliberately stores canonical JSONL rather than a vector-database
directory.  A derived index can be rebuilt locally from the portable artifact;
database implementation files are never part of the corpus release contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


FORMAT_VERSION = "1.0.0"
MODEL_IDENTITY_FIELDS = (
    "weights_digest",
    "tokenizer_digest",
    "runtime_digest",
    "dimension",
    "normalization",
    "quantization_digest",
)

_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_HEADER_FIELDS = {
    "record_type",
    "format_version",
    "model_identity",
    "model_identity_digest",
    "record_count",
}
_VECTOR_FIELDS = {
    "record_type",
    "chunk_id",
    "content_digest",
    "model_identity_digest",
    "vector",
    "vector_digest",
}


class EmbeddingLineageError(ValueError):
    """Raised when an embedding artifact is ambiguous, corrupt, or incompatible."""


@dataclass(frozen=True)
class EmbeddingArtifact:
    """Validated portable embedding data and the digest of its exact bytes."""

    model_identity: Mapping[str, Any]
    model_identity_digest: str
    records: Tuple[Mapping[str, Any], ...]
    aggregate_digest: str


def canonical_json(value: Any) -> str:
    """Canonical JSON used for all identities and JSONL records."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EmbeddingLineageError(f"value is not canonical JSON: {exc}") from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _validate_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise EmbeddingLineageError(f"{label} must be a lowercase sha256 digest")
    return value


def validate_model_identity(identity: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a normalized copy of a complete embedding model identity."""

    if not isinstance(identity, Mapping):
        raise EmbeddingLineageError("model identity must be an object")
    missing = sorted(set(MODEL_IDENTITY_FIELDS) - set(identity))
    unknown = sorted(set(identity) - set(MODEL_IDENTITY_FIELDS))
    if missing:
        raise EmbeddingLineageError(f"model identity is missing: {', '.join(missing)}")
    if unknown:
        raise EmbeddingLineageError(f"model identity has unknown fields: {', '.join(unknown)}")

    dimension = identity["dimension"]
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 1:
        raise EmbeddingLineageError("model identity dimension must be a positive integer")
    normalization = identity["normalization"]
    if normalization not in ("none", "l2"):
        raise EmbeddingLineageError("model identity normalization must be 'none' or 'l2'")
    normalized = {
        "weights_digest": _validate_digest(identity["weights_digest"], "weights_digest"),
        "tokenizer_digest": _validate_digest(identity["tokenizer_digest"], "tokenizer_digest"),
        "runtime_digest": _validate_digest(identity["runtime_digest"], "runtime_digest"),
        "dimension": dimension,
        "normalization": normalization,
        "quantization_digest": _validate_digest(
            identity["quantization_digest"], "quantization_digest"
        ),
    }
    return normalized


def model_identity_digest(identity: Mapping[str, Any]) -> str:
    """Hash a complete, normalized model identity."""

    normalized = validate_model_identity(identity)
    return _sha256_bytes(canonical_json(normalized).encode("utf-8"))


def _content_digest(content: str) -> str:
    return _sha256_bytes(content.encode("utf-8"))


def _normalize_chunks(chunks: Iterable[Mapping[str, Any]]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    seen = set()
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, Mapping):
            raise EmbeddingLineageError(f"chunk {index} must be an object")
        chunk_id = chunk.get("chunk_id")
        content = chunk.get("content")
        digest = chunk.get("content_digest")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise EmbeddingLineageError(f"chunk {index} has no chunk_id")
        if chunk_id in seen:
            raise EmbeddingLineageError(f"duplicate chunk_id: {chunk_id}")
        seen.add(chunk_id)
        if not isinstance(content, str):
            raise EmbeddingLineageError(f"chunk {chunk_id} content must be text")
        _validate_digest(digest, f"chunk {chunk_id} content_digest")
        if digest != _content_digest(content):
            raise EmbeddingLineageError(f"chunk {chunk_id} content digest mismatch")
        normalized.append(
            {"chunk_id": chunk_id, "content": content, "content_digest": digest}
        )
    normalized.sort(key=lambda item: item["chunk_id"])
    return normalized


def _normalize_vector(vector: Any, dimension: int, chunk_id: str) -> List[float]:
    if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
        raise EmbeddingLineageError(f"vector for {chunk_id} must be a sequence")
    if len(vector) != dimension:
        raise EmbeddingLineageError(
            f"vector for {chunk_id} has dimension {len(vector)}, expected {dimension}"
        )
    result = []
    for index, value in enumerate(vector):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingLineageError(f"vector for {chunk_id} has a non-numeric value at {index}")
        value = float(value)
        if not math.isfinite(value):
            raise EmbeddingLineageError(f"vector for {chunk_id} has a non-finite value at {index}")
        # Remove a representation-only distinction that has no vector meaning.
        result.append(0.0 if value == 0.0 else value)
    return result


def _validate_declared_normalization(
    vector: Sequence[float], normalization: str, chunk_id: str
) -> None:
    if normalization != "l2":
        return
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise EmbeddingLineageError(
            f"vector for {chunk_id} claims l2 normalization but has norm {norm}"
        )


def _vector_digest(
    chunk_id: str,
    content_digest: str,
    identity_digest: str,
    vector: Sequence[float],
) -> str:
    binding = {
        "chunk_id": chunk_id,
        "content_digest": content_digest,
        "model_identity_digest": identity_digest,
        "vector": list(vector),
    }
    return _sha256_bytes(canonical_json(binding).encode("utf-8"))


def build_embedding_records(
    chunks: Iterable[Mapping[str, Any]],
    model_identity: Mapping[str, Any],
    embedder: Callable[[Sequence[str]], Sequence[Sequence[float]]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Build canonical records using an injected, offline-capable embedder.

    Chunks are sorted before the embedder is called, making input iteration
    order irrelevant.  The embedder must return exactly one vector per text.
    """

    identity = validate_model_identity(model_identity)
    identity_digest = model_identity_digest(identity)
    normalized_chunks = _normalize_chunks(chunks)
    if not callable(embedder):
        raise EmbeddingLineageError("embedder must be callable")
    vectors = embedder([chunk["content"] for chunk in normalized_chunks])
    if isinstance(vectors, (str, bytes)) or not isinstance(vectors, Sequence):
        raise EmbeddingLineageError("embedder result must be a sequence")
    if len(vectors) != len(normalized_chunks):
        raise EmbeddingLineageError(
            f"embedder returned {len(vectors)} vectors for {len(normalized_chunks)} chunks"
        )

    records: List[Dict[str, Any]] = []
    for chunk, raw_vector in zip(normalized_chunks, vectors):
        vector = _normalize_vector(raw_vector, identity["dimension"], chunk["chunk_id"])
        _validate_declared_normalization(
            vector, identity["normalization"], chunk["chunk_id"]
        )
        records.append(
            {
                "record_type": "embedding_vector",
                "chunk_id": chunk["chunk_id"],
                "content_digest": chunk["content_digest"],
                "model_identity_digest": identity_digest,
                "vector": vector,
                "vector_digest": _vector_digest(
                    chunk["chunk_id"],
                    chunk["content_digest"],
                    identity_digest,
                    vector,
                ),
            }
        )

    header = {
        "record_type": "embedding_header",
        "format_version": FORMAT_VERSION,
        "model_identity": identity,
        "model_identity_digest": identity_digest,
        "record_count": len(records),
    }
    return header, records


def render_embedding_jsonl(
    chunks: Iterable[Mapping[str, Any]],
    model_identity: Mapping[str, Any],
    embedder: Callable[[Sequence[str]], Sequence[Sequence[float]]],
) -> bytes:
    """Render the exact canonical JSONL bytes for a portable artifact."""

    header, records = build_embedding_records(chunks, model_identity, embedder)
    lines = [canonical_json(header)] + [canonical_json(record) for record in records]
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_embedding_jsonl(
    path: Union[str, Path],
    chunks: Iterable[Mapping[str, Any]],
    model_identity: Mapping[str, Any],
    embedder: Callable[[Sequence[str]], Sequence[Sequence[float]]],
) -> str:
    """Write canonical JSONL and return its aggregate SHA-256 identity."""

    payload = render_embedding_jsonl(chunks, model_identity, embedder)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{0}.".format(destination.name), dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(destination))
        directory_fd = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return _sha256_bytes(payload)


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EmbeddingLineageError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_line(line: str, line_number: int) -> Dict[str, Any]:
    try:
        value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, EmbeddingLineageError) as exc:
        raise EmbeddingLineageError(f"invalid JSONL line {line_number}: {exc}") from exc
    if not isinstance(value, dict):
        raise EmbeddingLineageError(f"JSONL line {line_number} must be an object")
    if canonical_json(value) != line:
        raise EmbeddingLineageError(f"JSONL line {line_number} is not canonical JSON")
    return value


def _expected_chunk_digests(
    expected_chunks: Optional[Iterable[Mapping[str, Any]]],
) -> Optional[Dict[str, str]]:
    if expected_chunks is None:
        return None
    return {
        chunk["chunk_id"]: chunk["content_digest"]
        for chunk in _normalize_chunks(expected_chunks)
    }


def read_embedding_jsonl(
    path: Union[str, Path],
    expected_chunks: Optional[Iterable[Mapping[str, Any]]] = None,
    expected_aggregate_digest: Optional[str] = None,
) -> EmbeddingArtifact:
    """Read and fully validate a canonical portable embedding artifact."""

    payload = Path(path).read_bytes()
    aggregate_digest = _sha256_bytes(payload)
    if expected_aggregate_digest is not None:
        _validate_digest(expected_aggregate_digest, "expected aggregate digest")
        if aggregate_digest != expected_aggregate_digest:
            raise EmbeddingLineageError("embedding aggregate digest mismatch")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EmbeddingLineageError("embedding JSONL must be UTF-8") from exc
    if not text.endswith("\n"):
        raise EmbeddingLineageError("embedding JSONL must end with a newline")
    lines = text.splitlines()
    if not lines:
        raise EmbeddingLineageError("embedding JSONL is empty")

    header = _parse_line(lines[0], 1)
    if set(header) != _HEADER_FIELDS:
        raise EmbeddingLineageError("embedding header fields do not match the format")
    if header["record_type"] != "embedding_header":
        raise EmbeddingLineageError("first JSONL record must be an embedding header")
    if header["format_version"] != FORMAT_VERSION:
        raise EmbeddingLineageError("unsupported embedding format version")
    identity = validate_model_identity(header["model_identity"])
    identity_digest = model_identity_digest(identity)
    if header["model_identity_digest"] != identity_digest:
        raise EmbeddingLineageError("model identity digest mismatch")
    if not isinstance(header["record_count"], int) or isinstance(header["record_count"], bool):
        raise EmbeddingLineageError("embedding record_count must be an integer")
    if header["record_count"] != len(lines) - 1:
        raise EmbeddingLineageError("embedding record_count does not match JSONL records")

    expected = _expected_chunk_digests(expected_chunks)
    seen = set()
    previous_id: Optional[str] = None
    records: List[Mapping[str, Any]] = []
    for line_number, line in enumerate(lines[1:], start=2):
        record = _parse_line(line, line_number)
        if set(record) != _VECTOR_FIELDS:
            raise EmbeddingLineageError(f"embedding vector fields are invalid on line {line_number}")
        if record["record_type"] != "embedding_vector":
            raise EmbeddingLineageError(f"invalid record type on line {line_number}")
        chunk_id = record["chunk_id"]
        if not isinstance(chunk_id, str) or not chunk_id:
            raise EmbeddingLineageError(f"invalid chunk_id on line {line_number}")
        if chunk_id in seen:
            raise EmbeddingLineageError(f"duplicate chunk_id: {chunk_id}")
        if previous_id is not None and chunk_id <= previous_id:
            raise EmbeddingLineageError("embedding records are not in canonical chunk_id order")
        seen.add(chunk_id)
        previous_id = chunk_id
        content_digest = _validate_digest(record["content_digest"], f"{chunk_id} content_digest")
        if record["model_identity_digest"] != identity_digest:
            raise EmbeddingLineageError(f"model identity mismatch for chunk {chunk_id}")
        vector = _normalize_vector(record["vector"], identity["dimension"], chunk_id)
        expected_vector_digest = _vector_digest(
            chunk_id, content_digest, identity_digest, vector
        )
        if record["vector_digest"] != expected_vector_digest:
            raise EmbeddingLineageError(f"vector digest mismatch for chunk {chunk_id}")
        _validate_declared_normalization(vector, identity["normalization"], chunk_id)
        if expected is not None:
            if chunk_id not in expected:
                raise EmbeddingLineageError(f"unexpected embedding chunk: {chunk_id}")
            if expected[chunk_id] != content_digest:
                raise EmbeddingLineageError(f"content digest mismatch for chunk {chunk_id}")
        records.append({**record, "vector": vector})

    if expected is not None and seen != set(expected):
        missing = sorted(set(expected) - seen)
        raise EmbeddingLineageError(
            "embedding artifact is missing chunks: " + ", ".join(missing)
        )
    return EmbeddingArtifact(
        model_identity=identity,
        model_identity_digest=identity_digest,
        records=tuple(records),
        aggregate_digest=aggregate_digest,
    )
