"""Deterministic, offline building blocks for the Big EVY corpus factory."""

from .pipeline import (
    CanonicalDocumentSpec,
    EvidenceSnapshot,
    FactoryResult,
    FileSourceAcquirer,
    SourceRecordSpec,
    build_canonical_document,
    build_chunks,
    build_from_file,
    sha256_digest,
)

__all__ = [
    "CanonicalDocumentSpec",
    "EvidenceSnapshot",
    "FactoryResult",
    "FileSourceAcquirer",
    "SourceRecordSpec",
    "build_canonical_document",
    "build_chunks",
    "build_from_file",
    "sha256_digest",
]
