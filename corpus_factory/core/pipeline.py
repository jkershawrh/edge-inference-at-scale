"""Deterministic source-to-chunk corpus construction.

The core deliberately supports only local files. Network connectors can stage an
input file separately, but this trust boundary will only read a regular file
whose resolved path is contained by an explicit allowlisted root. Evidence is
stored by content digest so a rebuild never mutates an earlier snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple, Union
from urllib.parse import unquote, urlparse

from corpus_factory.validator import validate_instance


Pathish = Union[str, os.PathLike[str]]
SCHEMA_VERSION = "2.0.0"
CHUNKER_NAME = "evy-unicode-span"
CHUNKER_VERSION = "1.0.0"


def sha256_digest(value: bytes) -> str:
    """Return the contract's canonical SHA-256 representation."""

    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    return "{0}-{1}".format(prefix, hashlib.sha256(_canonical_bytes(value)).hexdigest()[:24])


def _deduplicated(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


@dataclass(frozen=True)
class SourceRecordSpec:
    """Human-supplied governance metadata for one acquired source."""

    source_id: str
    publisher_id: str
    publisher_name: str
    steward_identity: str
    steward_role: str
    authority_class: str
    connector_id: str
    acquisition_policy_version: str
    acquired_at: str
    last_verified_at: str
    license: str
    redistribution: str
    geographies: Tuple[str, ...]
    languages: Tuple[str, ...]
    audiences: Tuple[str, ...]
    subjects: Tuple[str, ...]
    effective_from: str
    valid_until: str | None
    expected_refresh_seconds: int
    stale_action: str
    media_type: str = "text/plain"
    restrictions: Tuple[str, ...] = ()
    sensitivity: str = "public"
    distribution: str = "deployment"


@dataclass(frozen=True)
class CanonicalDocumentSpec:
    """Deployment and review metadata applied to canonical evidence text."""

    document_id: str
    event_id: str
    deployment_ids: Tuple[str, ...]
    geographies: Tuple[str, ...]
    languages: Tuple[str, ...]
    audiences: Tuple[str, ...]
    valid_from: str
    valid_until: str | None
    review_due_at: str
    stale_action: str
    authority_rank: int
    safety_class: str
    permitted_delivery_channels: Tuple[str, ...]
    review_attestation_ids: Tuple[str, ...]
    approval_state: str = "draft"
    revision: int = 1


@dataclass(frozen=True)
class EvidenceSnapshot:
    """An immutable content-addressed copy plus its validated source record."""

    digest: str
    path: Path
    source_record: Dict[str, Any]


@dataclass(frozen=True)
class FactoryResult:
    source_record: Dict[str, Any]
    canonical_document: Dict[str, Any]
    chunks: Tuple[Dict[str, Any], ...]
    evidence_path: Path


class FileSourceAcquirer:
    """Acquire files under allowlisted roots into an immutable evidence store."""

    def __init__(self, allowlisted_roots: Sequence[Pathish], evidence_store: Pathish):
        if not allowlisted_roots:
            raise ValueError("at least one allowlisted source root is required")
        self._roots = tuple(Path(root).resolve(strict=True) for root in allowlisted_roots)
        for root in self._roots:
            if not root.is_dir():
                raise ValueError("allowlisted source root is not a directory: {0}".format(root))
        self._evidence_store = Path(evidence_store).resolve()

    @staticmethod
    def _path_from_reference(reference: Pathish) -> Path:
        raw = os.fspath(reference)
        parsed = urlparse(raw)
        if parsed.scheme:
            if parsed.scheme != "file":
                raise ValueError("only local paths and file:// sources are supported")
            if parsed.netloc not in ("", "localhost"):
                raise ValueError("remote file URI authorities are not supported")
            return Path(unquote(parsed.path))
        return Path(raw)

    def _resolve_allowed(self, reference: Pathish) -> Path:
        candidate = self._path_from_reference(reference).resolve(strict=True)
        if not candidate.is_file():
            raise ValueError("source must be a regular file: {0}".format(candidate))
        if not any(candidate.is_relative_to(root) for root in self._roots):
            raise PermissionError("source is outside the allowlisted roots: {0}".format(candidate))
        return candidate

    def _snapshot(self, payload: bytes, digest: str) -> Path:
        hex_digest = digest.removeprefix("sha256:")
        snapshot_dir = self._evidence_store / "sha256" / hex_digest
        snapshot_path = snapshot_dir / "payload"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        if snapshot_path.exists():
            if snapshot_path.read_bytes() != payload:
                raise RuntimeError("immutable evidence snapshot digest collision")
            return snapshot_path

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=snapshot_dir, delete=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            try:
                os.link(temporary, snapshot_path)
            except FileExistsError:
                if snapshot_path.read_bytes() != payload:
                    raise RuntimeError("immutable evidence snapshot digest collision")
            snapshot_path.chmod(0o444)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return snapshot_path

    def acquire(self, reference: Pathish, spec: SourceRecordSpec) -> EvidenceSnapshot:
        source_path = self._resolve_allowed(reference)
        payload = source_path.read_bytes()
        if not payload:
            raise ValueError("empty source evidence is not permitted")
        digest = sha256_digest(payload)
        snapshot_path = self._snapshot(payload, digest)
        locator_reference = source_path.as_uri()
        record: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "source_record",
            "source_id": spec.source_id,
            "publisher": {"id": spec.publisher_id, "name": spec.publisher_name},
            "steward": {"identity": spec.steward_identity, "role": spec.steward_role},
            "authority_class": spec.authority_class,
            "locator": {
                "kind": "file",
                "reference": locator_reference,
                "acquisition_method": "manual_upload",
                "connector_id": spec.connector_id,
                "acquisition_policy_version": spec.acquisition_policy_version,
            },
            "acquired_at": spec.acquired_at,
            "last_verified_at": spec.last_verified_at,
            "evidence": {
                "media_type": spec.media_type,
                "byte_size": len(payload),
                "digest": digest,
            },
            "rights": {
                "license": spec.license,
                "redistribution": spec.redistribution,
                "restrictions": _deduplicated(spec.restrictions),
            },
            "scope": {
                "geographies": _deduplicated(spec.geographies),
                "languages": _deduplicated(spec.languages),
                "audiences": _deduplicated(spec.audiences),
                "subjects": _deduplicated(spec.subjects),
            },
            "freshness": {
                "effective_from": spec.effective_from,
                "valid_until": spec.valid_until,
                "expected_refresh_seconds": spec.expected_refresh_seconds,
                "stale_action": spec.stale_action,
            },
            "sensitivity": spec.sensitivity,
            "distribution": spec.distribution,
        }
        validate_instance(record, "source_record")
        return EvidenceSnapshot(digest=digest, path=snapshot_path, source_record=record)


def build_canonical_document(
    snapshot: EvidenceSnapshot, spec: CanonicalDocumentSpec
) -> Dict[str, Any]:
    """Decode an evidence snapshot without changing its code-point lineage."""

    payload = snapshot.path.read_bytes()
    if sha256_digest(payload) != snapshot.digest:
        raise ValueError("evidence snapshot no longer matches its digest")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("canonical text sources must be valid UTF-8") from exc
    if not text:
        raise ValueError("canonical text cannot be empty")

    source = snapshot.source_record
    citation = {
        "source_id": source["source_id"],
        "evidence_digest": snapshot.digest,
        "location": "unicode_code_point:0-{0}".format(len(text)),
    }
    document_digest = sha256_digest(text.encode("utf-8"))
    document: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "canonical_document",
        "document_id": spec.document_id,
        "revision": spec.revision,
        "document_digest": document_digest,
        "event_id": spec.event_id,
        "deployment_ids": _deduplicated(spec.deployment_ids),
        "scope": {
            "geographies": _deduplicated(spec.geographies),
            "languages": _deduplicated(spec.languages),
            "audiences": _deduplicated(spec.audiences),
        },
        "canonical_text": text,
        "structured_facts": [],
        "source_citations": [citation],
        "transformations": [
            {"tool": "evy-canonical-text", "version": "1.0.0", "operation": "utf8-decode"}
        ],
        "validity": {
            "valid_from": spec.valid_from,
            "valid_until": spec.valid_until,
            "review_due_at": spec.review_due_at,
            "stale_action": spec.stale_action,
        },
        "authority_rank": spec.authority_rank,
        "conflict": {"state": "none", "explanation": "", "supersedes": []},
        "safety_class": spec.safety_class,
        "permitted_delivery_channels": _deduplicated(spec.permitted_delivery_channels),
        "review_attestation_ids": _deduplicated(spec.review_attestation_ids),
        "approval_state": spec.approval_state,
    }
    validate_instance(document, "canonical_document")
    return document


_HEADING = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")


def _section_path_at(text: str, offset: int) -> list[str]:
    path: list[str] = []
    for match in _HEADING.finditer(text):
        if match.start() > offset:
            break
        level = len(match.group(1))
        title = match.group(2)
        path = path[: level - 1]
        path.append(title)
    return path


def _chunk_spans(text: str, maximum: int) -> list[tuple[int, int]]:
    if maximum < 1:
        raise ValueError("max_code_points must be positive")
    spans: list[tuple[int, int]] = []
    heading_starts = tuple(match.start() for match in _HEADING.finditer(text))
    cursor = 0
    length = len(text)
    while cursor < length:
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break
        hard_end = min(cursor + maximum, length)
        next_heading = next(
            (start for start in heading_starts if cursor < start < hard_end), None
        )
        if next_heading is not None:
            hard_end = next_heading
        end = hard_end
        if hard_end < length:
            split = max(text.rfind("\n", cursor + 1, hard_end + 1), text.rfind(" ", cursor + 1, hard_end + 1), text.rfind("\t", cursor + 1, hard_end + 1))
            if split > cursor:
                end = split
        while end > cursor and text[end - 1].isspace():
            end -= 1
        if end == cursor:
            end = hard_end
        spans.append((cursor, end))
        cursor = end
    return spans


def build_chunks(
    document: Mapping[str, Any], max_code_points: int = 800
) -> Tuple[Dict[str, Any], ...]:
    """Split canonical text into deterministic chunks with exact parent spans."""

    validate_instance(document, "canonical_document")
    text = document["canonical_text"]
    configuration = {"max_code_points": max_code_points, "split_preference": "newline-whitespace"}
    code_digest = sha256_digest(
        _canonical_bytes(
            {"name": CHUNKER_NAME, "version": CHUNKER_VERSION, "algorithm": "unicode-span-v1"}
        )
    )
    records = []
    for start, end in _chunk_spans(text, max_code_points):
        content = text[start:end]
        content_digest = sha256_digest(content.encode("utf-8"))
        section_path = _section_path_at(text, start)
        identity = {
            "document_digest": document["document_digest"],
            "start": start,
            "end": end,
            "content_digest": content_digest,
            "chunker_code_digest": code_digest,
            "configuration": configuration,
        }
        record: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "chunk_record",
            "chunk_id": _stable_id("chk", identity),
            "parent": {
                "document_id": document["document_id"],
                "revision": document["revision"],
                "document_digest": document["document_digest"],
            },
            "content": content,
            "content_digest": content_digest,
            "source_span": {"unit": "unicode_code_point", "start": start, "end": end},
            "section_path": section_path,
            "chunker": {
                "name": CHUNKER_NAME,
                "version": CHUNKER_VERSION,
                "configuration": configuration,
                "code_digest": code_digest,
            },
            "language": document["scope"]["languages"][0],
            "token_count": len(re.findall(r"\S+", content)),
            "scope": {
                "event_id": document["event_id"],
                "geographies": list(document["scope"]["geographies"]),
                "audiences": list(document["scope"]["audiences"]),
            },
        }
        validate_instance(record, "chunk_record")
        records.append(record)
    if not records:
        raise ValueError("canonical document produced no non-whitespace chunks")
    return tuple(records)


def build_from_file(
    reference: Pathish,
    *,
    allowlisted_roots: Sequence[Pathish],
    evidence_store: Pathish,
    source_spec: SourceRecordSpec,
    document_spec: CanonicalDocumentSpec,
    max_code_points: int = 800,
) -> FactoryResult:
    """Run the deterministic offline acquisition, canonicalization and chunking flow."""

    snapshot = FileSourceAcquirer(allowlisted_roots, evidence_store).acquire(
        reference, source_spec
    )
    document = build_canonical_document(snapshot, document_spec)
    chunks = build_chunks(document, max_code_points=max_code_points)
    return FactoryResult(snapshot.source_record, document, chunks, snapshot.path)
