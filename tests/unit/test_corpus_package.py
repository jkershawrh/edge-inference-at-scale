"""Tests for immutable event corpus validation and signing."""

import base64
from argparse import Namespace
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.services.rag_service.corpus_package import (
    CorpusValidationError,
    validate_corpus_package,
)
from scripts.package_corpus import build_package


def _write_package(tmp_path, signed=False):
    text = "Emergency shelter is at the school."
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    documents = {
        "doc-1": {
            "id": "doc-1",
            "text": text,
            "category": "emergency",
            "content_hash": content_hash,
        }
    }
    documents_path = tmp_path / "documents.json"
    documents_path.write_text(json.dumps(documents), encoding="utf-8")
    categories_path = tmp_path / "categories.json"
    categories_path.write_text(
        json.dumps({"categories": ["emergency"]}), encoding="utf-8"
    )
    index_path = tmp_path / "document_index.json"
    index_path.write_text(json.dumps({"doc-1": content_hash}), encoding="utf-8")
    files = {}
    for file_path in (documents_path, categories_path, index_path):
        files[file_path.name] = {
            "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(),
            "bytes": file_path.stat().st_size,
        }
    manifest = {
        "schema_version": "1.0",
        "event": {"id": "flood-response", "name": "Flood Response"},
        "corpus": {"version": "2026.3", "document_count": 1},
        "files": files,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    public_key_path = None
    if signed:
        private_key = Ed25519PrivateKey.generate()
        signature = private_key.sign(manifest_path.read_bytes())
        (tmp_path / "manifest.sig").write_text(
            base64.b64encode(signature).decode("ascii"), encoding="ascii"
        )
        public_key_path = tmp_path / "public-key.pem"
        public_key_path.write_bytes(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    return manifest_path, public_key_path


def test_validates_event_version_hashes_and_signature(tmp_path):
    manifest_path, public_key_path = _write_package(tmp_path, signed=True)

    manifest = validate_corpus_package(
        str(manifest_path),
        expected_event_id="flood-response",
        expected_version="2026.3",
        public_key_path=str(public_key_path),
        require_signature=True,
    )

    assert manifest["corpus"]["document_count"] == 1


def test_rejects_file_tampering(tmp_path):
    manifest_path, _ = _write_package(tmp_path)
    (tmp_path / "documents.json").write_text("{}", encoding="utf-8")

    with pytest.raises(CorpusValidationError, match="size mismatch|hash mismatch"):
        validate_corpus_package(str(manifest_path))


def test_rejects_wrong_event_or_version(tmp_path):
    manifest_path, _ = _write_package(tmp_path)

    with pytest.raises(CorpusValidationError, match="event ID"):
        validate_corpus_package(str(manifest_path), expected_event_id="other-event")
    with pytest.raises(CorpusValidationError, match="version"):
        validate_corpus_package(str(manifest_path), expected_version="2026.4")


def test_requires_signature_when_policy_is_enabled(tmp_path):
    manifest_path, _ = _write_package(tmp_path)

    with pytest.raises(CorpusValidationError, match="manifest.sig"):
        validate_corpus_package(str(manifest_path), require_signature=True)


def _builder_args(tmp_path, input_path, version="v1"):
    return Namespace(
        event_id="rural-clinic",
        event_name="Rural Clinic Deployment",
        version=version,
        output_dir=str(tmp_path / "output"),
        created_at="2026-10-06T00:00:00+00:00",
        signing_key=None,
        input_documents=str(input_path),
    )


def test_builder_supports_event_neutral_documents(tmp_path):
    input_path = tmp_path / "clinic.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "id": "clinic-hours",
                    "text": "The mobile clinic opens at 08:00.",
                    "metadata": {"category": "health"},
                }
            ]
        ),
        encoding="utf-8",
    )

    package = build_package(_builder_args(tmp_path, input_path))
    manifest = validate_corpus_package(
        str(package / "manifest.json"), "rural-clinic", "v1"
    )

    assert manifest["corpus"]["document_count"] == 1
    assert manifest["sources"][0]["name"] == "clinic.json"


def test_builder_rejects_unknown_document_safety_class(tmp_path):
    input_path = tmp_path / "unsafe-class.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "id": "clinic-hours",
                    "text": "The mobile clinic opens at 08:00.",
                    "metadata": {
                        "category": "health",
                        "safety_class": "urgent-ish",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="safety_class"):
        build_package(_builder_args(tmp_path, input_path))


def test_failed_build_leaves_no_partial_version(tmp_path):
    input_path = tmp_path / "duplicate.json"
    input_path.write_text(
        json.dumps(
            [
                {"id": "same", "text": "one"},
                {"id": "same", "text": "two"},
            ]
        ),
        encoding="utf-8",
    )
    args = _builder_args(tmp_path, input_path, version="broken")

    with pytest.raises(ValueError, match="duplicate"):
        build_package(args)

    event_dir = tmp_path / "output" / "rural-clinic"
    assert not (event_dir / "broken").exists()
    assert list(event_dir.glob(".broken.staging-*")) == []


def test_builder_signs_manifest_with_ed25519_key(tmp_path):
    input_path = tmp_path / "event.json"
    input_path.write_text(
        json.dumps([{"id": "notice", "text": "Water distribution begins at noon."}]),
        encoding="utf-8",
    )
    private_key = Ed25519PrivateKey.generate()
    private_key_path = tmp_path / "private-key.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key_path = tmp_path / "public-key.pem"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    args = _builder_args(tmp_path, input_path, version="signed")
    args.signing_key = str(private_key_path)

    package = build_package(args)

    validate_corpus_package(
        str(package / "manifest.json"),
        expected_event_id="rural-clinic",
        expected_version="signed",
        public_key_path=str(public_key_path),
        require_signature=True,
    )
