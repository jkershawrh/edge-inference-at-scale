"""Deterministic evidence lineage for the checked-in Summit Connect corpus.

This module stops before approval and release.  It binds exact source bytes to
canonical JSON identities and the mission-scoped document classifications, but
does not invent structured facts, review attestations, or approval decisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping

from corpus_factory.validator import ContractValidationError, validate_instance


SCHEMA_VERSION = "1.0.0"
RECORD_TYPE = "corpus_lineage_manifest"
TRANSFORM_VERSION = "1.0.0"
DEFAULT_SOURCE_ROOT = "data/summit_connect"
DEFAULT_EXCLUSIONS = {
    "architecture.json": (
        "Product architecture and demo claims are outside the Summit attendee "
        "information mission profile."
    )
}


class SummitLineageError(ValueError):
    """Raised when Summit source evidence cannot be linked unambiguously."""


def canonical_json(value: Any) -> str:
    """Serialize JSON with a stable byte representation."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SummitLineageError(f"value is not canonical JSON: {exc}") from exc


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SummitLineageError(f"cannot read JSON from {path}: {exc}") from exc


def _document_id(path: Path) -> str:
    return "document-summit-" + path.stem.replace("_", "-")


def _classification_index(document: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    records = document.get("classifications")
    if not isinstance(records, list):
        raise SummitLineageError("classification document must contain a classifications array")

    result: Dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise SummitLineageError(f"classification {index} must be an object")
        try:
            validate_instance(record, "document_classification")
        except ContractValidationError as exc:
            raise SummitLineageError(
                f"classification {index} fails its contract: {exc}"
            ) from exc
        reference = record.get("document")
        if not isinstance(reference, Mapping):
            raise SummitLineageError(f"classification {index} has no document reference")
        document_id = reference.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise SummitLineageError(f"classification {index} has no document_id")
        if document_id in result:
            raise SummitLineageError(f"duplicate classification for {document_id}")
        result[document_id] = record
    return result


def _approved_sources(registry: Mapping[str, Any]) -> set[str]:
    records = registry.get("sources")
    if not isinstance(records, list):
        raise SummitLineageError("source registry must contain a sources array")

    approved: set[str] = set()
    for index, source in enumerate(records):
        if not isinstance(source, Mapping):
            raise SummitLineageError(f"registry source {index} must be an object")
        source_id = source.get("source_id")
        approval = source.get("approval")
        if (
            source.get("enabled") is True
            and isinstance(source_id, str)
            and isinstance(approval, Mapping)
            and approval.get("status") == "approved"
        ):
            approved.add(source_id)
    if not approved:
        raise SummitLineageError("source registry contains no enabled approved source")
    return approved


def build_summit_lineage(
    data_dir: Path | str,
    registry_path: Path | str,
    classifications_path: Path | str,
    *,
    source_root: str = DEFAULT_SOURCE_ROOT,
    exclusions: Mapping[str, str] = DEFAULT_EXCLUSIONS,
) -> Dict[str, Any]:
    """Build exact-byte and canonical-JSON lineage without granting approval.

    Every JSON file must have exactly one classification or an explicit
    exclusion.  Conversely, every classification must resolve to one file.
    """

    data_dir = Path(data_dir)
    registry_path = Path(registry_path)
    classifications_path = Path(classifications_path)
    registry = _load_json(registry_path)
    classifications_document = _load_json(classifications_path)
    if not isinstance(registry, Mapping) or not isinstance(classifications_document, Mapping):
        raise SummitLineageError("registry and classifications must be JSON objects")
    try:
        validate_instance(registry, "source_registry")
    except ContractValidationError as exc:
        raise SummitLineageError(f"source registry fails its contract: {exc}") from exc

    registry_id = registry.get("registry_id")
    event_id = registry.get("event_id")
    if not isinstance(registry_id, str) or not isinstance(event_id, str):
        raise SummitLineageError("source registry must identify its registry and event")
    classification_index = _classification_index(classifications_document)
    approved_sources = _approved_sources(registry)

    files = sorted(data_dir.glob("*.json"), key=lambda item: item.name)
    if not files:
        raise SummitLineageError(f"no JSON evidence found in {data_dir}")

    records = []
    resolved_documents: set[str] = set()
    for path in files:
        document_id = _document_id(path)
        classification = classification_index.get(document_id)
        exclusion_reason = exclusions.get(path.name)
        if classification is None and exclusion_reason is None:
            raise SummitLineageError(
                f"{path.name} has neither a document classification nor an explicit exclusion"
            )
        if classification is not None and exclusion_reason is not None:
            raise SummitLineageError(
                f"{path.name} cannot be both classified and explicitly excluded"
            )

        raw_bytes = path.read_bytes()
        value = _load_json(path)
        canonical_bytes = canonical_json(value).encode("utf-8")
        relative_path = f"{source_root.rstrip('/')}/{path.name}"

        if classification is not None:
            resolved_documents.add(document_id)
            reference = classification["document"]
            revision = reference.get("revision")
            classification_id = classification.get("classification_id")
            mission_profile_id = classification.get("mission_profile_id")
            provenance = classification.get("provenance")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise SummitLineageError(f"classification for {document_id} has invalid revision")
            if not isinstance(classification_id, str) or not isinstance(mission_profile_id, str):
                raise SummitLineageError(f"classification for {document_id} is missing identity")
            if not isinstance(provenance, Mapping):
                raise SummitLineageError(f"classification for {document_id} has no provenance")
            source_ids = provenance.get("source_ids")
            if not isinstance(source_ids, list) or not source_ids:
                raise SummitLineageError(f"classification for {document_id} has no source_ids")
            unknown_sources = sorted(set(source_ids) - approved_sources)
            if unknown_sources:
                raise SummitLineageError(
                    f"classification for {document_id} references unapproved source(s): "
                    + ", ".join(unknown_sources)
                )
            classification_digest = _digest_bytes(
                canonical_json(classification).encode("utf-8")
            )
            state = "classified"
        else:
            revision = 1
            classification_id = None
            mission_profile_id = None
            # The registry describes the checked-in fixture bundle as one
            # publisher export.  Exclusion limits use; it does not erase the
            # evidence's source lineage.
            source_ids = sorted(approved_sources)
            classification_digest = None
            state = "excluded"

        records.append(
            {
                "document": {
                    "document_id": document_id,
                    "revision": revision,
                    "canonical_digest": _digest_bytes(canonical_bytes),
                    "canonical_media_type": "application/json",
                },
                "classification": {
                    "classification_id": classification_id,
                    "classification_digest": classification_digest,
                    "mission_profile_id": mission_profile_id,
                },
                "evidence": {
                    "path": relative_path,
                    "media_type": "application/json",
                    "byte_size": len(raw_bytes),
                    "digest": _digest_bytes(raw_bytes),
                    "source_ids": source_ids,
                    "location": "$",
                },
                "transformation": {
                    "tool": "evy-summit-json-lineage",
                    "version": TRANSFORM_VERSION,
                    "operation": "canonical-json-sortkeys-v1",
                },
                "lifecycle": {
                    "state": state,
                    "approval_state": "not_evaluated",
                    "exclusion_reason": exclusion_reason,
                },
            }
        )

    unresolved = sorted(set(classification_index) - resolved_documents)
    if unresolved:
        raise SummitLineageError(
            "classifications reference missing Summit documents: " + ", ".join(unresolved)
        )

    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "event_id": event_id,
        "source_registry": {
            "registry_id": registry_id,
            "digest": _digest_bytes(canonical_json(registry).encode("utf-8")),
        },
        "classification_set_digest": _digest_bytes(
            canonical_json(classifications_document).encode("utf-8")
        ),
        "records": records,
    }
    manifest["manifest_digest"] = _digest_bytes(canonical_json(manifest).encode("utf-8"))
    return manifest


def render_summit_lineage(manifest: Mapping[str, Any]) -> bytes:
    """Render a manifest in its stable on-disk form."""

    return (canonical_json(manifest) + "\n").encode("utf-8")


def write_summit_lineage(path: Path | str, manifest: Mapping[str, Any]) -> None:
    """Atomically write a deterministic lineage manifest."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = render_summit_lineage(manifest)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def verify_summit_lineage(
    manifest: Mapping[str, Any],
    data_dir: Path | str,
    registry_path: Path | str,
    classifications_path: Path | str,
    *,
    source_root: str = DEFAULT_SOURCE_ROOT,
    exclusions: Mapping[str, str] = DEFAULT_EXCLUSIONS,
) -> None:
    """Fail if a manifest differs from a fresh build over current inputs."""

    expected = build_summit_lineage(
        data_dir,
        registry_path,
        classifications_path,
        source_root=source_root,
        exclusions=exclusions,
    )
    if canonical_json(manifest) != canonical_json(expected):
        raise SummitLineageError("lineage manifest does not match current evidence")
