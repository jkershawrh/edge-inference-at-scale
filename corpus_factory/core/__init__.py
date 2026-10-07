"""Deterministic, offline building blocks for the Big EVY corpus factory."""

from .pipeline import (
    CanonicalDocumentSpec,
    EvidenceVault,
    EvidenceSnapshot,
    FactoryResult,
    FileSourceAcquirer,
    SourceRecordSpec,
    build_source_record,
    build_canonical_document,
    build_chunks,
    build_from_file,
    sha256_digest,
)

__all__ = [
    "CanonicalDocumentSpec",
    "EvidenceVault",
    "EvidenceSnapshot",
    "FactoryResult",
    "FileSourceAcquirer",
    "SourceRecordSpec",
    "build_source_record",
    "build_canonical_document",
    "build_chunks",
    "build_from_file",
    "sha256_digest",
]
