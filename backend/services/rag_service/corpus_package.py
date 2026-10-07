"""Validation for immutable, event-scoped corpus packages."""

import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA_VERSION = "1.0"
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
SAFETY_CLASSES = {"advisory", "standard", "high", "critical"}


class CorpusValidationError(ValueError):
    """Raised when a corpus package fails integrity or identity checks."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_signature(manifest_bytes: bytes, signature_path: Path, public_key_path: Path) -> None:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise CorpusValidationError("cryptography is required for signed corpora") from exc

    try:
        signature = base64.b64decode(
            signature_path.read_text(encoding="ascii").strip(), validate=True
        )
        key_bytes = public_key_path.read_bytes()
        try:
            public_key = serialization.load_pem_public_key(key_bytes)
        except ValueError:
            public_key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(key_bytes.strip(), validate=True)
            )
    except (OSError, UnicodeDecodeError, ValueError, binascii.Error) as exc:
        raise CorpusValidationError("corpus signature or public key is malformed") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise CorpusValidationError("corpus public key is not Ed25519")
    try:
        public_key.verify(signature, manifest_bytes)
    except Exception as exc:
        raise CorpusValidationError("corpus manifest signature is invalid") from exc


def validate_corpus_package(
    manifest_path: str,
    expected_event_id: Optional[str] = None,
    expected_version: Optional[str] = None,
    public_key_path: Optional[str] = None,
    require_signature: bool = False,
) -> Dict[str, Any]:
    """Validate package schema, identity, file hashes, and optional signature."""
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise CorpusValidationError("corpus manifest does not exist: {0}".format(path))
    manifest_bytes = path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusValidationError("corpus manifest is not valid UTF-8 JSON") from exc

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise CorpusValidationError("unsupported corpus schema version")
    event = manifest.get("event") or {}
    corpus = manifest.get("corpus") or {}
    if not IDENTIFIER.fullmatch(str(event.get("id", ""))):
        raise CorpusValidationError("corpus event ID is missing or invalid")
    if not isinstance(event.get("name"), str) or not event["name"].strip():
        raise CorpusValidationError("corpus event name is missing")
    if not IDENTIFIER.fullmatch(str(corpus.get("version", ""))):
        raise CorpusValidationError("corpus version is missing or invalid")
    if not isinstance(corpus.get("document_count"), int) or corpus["document_count"] < 0:
        raise CorpusValidationError("corpus document count is invalid")
    if expected_event_id and event.get("id") != expected_event_id:
        raise CorpusValidationError("corpus event ID does not match deployment")
    if expected_version and corpus.get("version") != expected_version:
        raise CorpusValidationError("corpus version does not match deployment")

    package_root = path.parent
    files = manifest.get("files") or {}
    if not files:
        raise CorpusValidationError("corpus manifest contains no files")
    required_files = {"documents.json", "categories.json", "document_index.json"}
    if not required_files.issubset(files):
        raise CorpusValidationError("corpus manifest is missing required package files")
    for relative_name, expected in files.items():
        if not isinstance(relative_name, str) or not isinstance(expected, dict):
            raise CorpusValidationError("corpus file manifest entry is invalid")
        if not isinstance(expected.get("bytes"), int) or expected["bytes"] < 0:
            raise CorpusValidationError("corpus file size is invalid: {0}".format(relative_name))
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected.get("sha256", ""))):
            raise CorpusValidationError("corpus file hash is invalid: {0}".format(relative_name))
        file_path = (package_root / relative_name).resolve()
        try:
            file_path.relative_to(package_root)
        except ValueError as exc:
            raise CorpusValidationError("corpus file escapes package directory") from exc
        if not file_path.is_file():
            raise CorpusValidationError("corpus file is missing: {0}".format(relative_name))
        if file_path.stat().st_size != expected.get("bytes"):
            raise CorpusValidationError("corpus file size mismatch: {0}".format(relative_name))
        if _sha256(file_path) != expected.get("sha256"):
            raise CorpusValidationError("corpus file hash mismatch: {0}".format(relative_name))

    documents_path = package_root / "documents.json"
    if not documents_path.is_file():
        raise CorpusValidationError("corpus package must include documents.json")
    try:
        documents = json.loads(documents_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusValidationError("documents.json is not valid UTF-8 JSON") from exc
    if not isinstance(documents, dict):
        raise CorpusValidationError("documents.json must be keyed by document ID")
    if len(documents) != corpus.get("document_count"):
        raise CorpusValidationError("corpus document count does not match manifest")
    for document_id, document in documents.items():
        if not isinstance(document, dict) or document.get("id") != document_id:
            raise CorpusValidationError("invalid document record: {0}".format(document_id))
        if not isinstance(document.get("text"), str) or not document["text"].strip():
            raise CorpusValidationError("document has no text: {0}".format(document_id))
        actual_content_hash = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
        if document.get("content_hash") != actual_content_hash:
            raise CorpusValidationError("document content hash is invalid: {0}".format(document_id))
        if not isinstance(document.get("category"), str) or not document["category"]:
            raise CorpusValidationError("document category is invalid: {0}".format(document_id))
        metadata = document.get("metadata") or {}
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, (str, int, float, bool))
            for key, value in metadata.items()
        ):
            raise CorpusValidationError("document metadata is invalid: {0}".format(document_id))
        if (
            "safety_class" in metadata
            and metadata["safety_class"] not in SAFETY_CLASSES
        ):
            raise CorpusValidationError(
                "document safety class is invalid: {0}".format(document_id)
            )

    try:
        categories_value = json.loads(
            (package_root / "categories.json").read_text(encoding="utf-8")
        )
        index = json.loads(
            (package_root / "document_index.json").read_text(encoding="utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusValidationError("corpus category or index file is invalid JSON") from exc
    if not isinstance(categories_value, dict) or not isinstance(index, dict):
        raise CorpusValidationError("corpus category or index structure is invalid")
    expected_categories = sorted(
        {document.get("category", "general") for document in documents.values()}
    )
    if sorted(categories_value.get("categories", [])) != expected_categories:
        raise CorpusValidationError("corpus categories do not match documents")
    expected_index = {
        document_id: document.get("content_hash")
        or hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
        for document_id, document in documents.items()
    }
    if index != expected_index:
        raise CorpusValidationError("corpus document index does not match documents")

    signature_path = package_root / "manifest.sig"
    if require_signature and not signature_path.is_file():
        raise CorpusValidationError("signed corpus is required but manifest.sig is missing")
    if signature_path.is_file():
        if not public_key_path:
            if require_signature:
                raise CorpusValidationError("signed corpus requires a trusted public key")
        else:
            _verify_signature(manifest_bytes, signature_path, Path(public_key_path))

    return manifest
