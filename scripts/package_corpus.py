#!/usr/bin/env python3
"""Build an immutable, event-scoped corpus package."""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    from scripts.build_summit_corpus import (
        build_architecture_docs,
        build_city_docs,
        build_schedule_docs,
        build_session_docs,
        build_speaker_docs,
        build_venue_docs,
        DATA_DIR,
        load_json,
    )
except ImportError:  # Direct execution sets scripts/ as sys.path[0].
    from build_summit_corpus import (
        build_architecture_docs,
        build_city_docs,
        build_schedule_docs,
        build_session_docs,
        build_speaker_docs,
        build_venue_docs,
        DATA_DIR,
        load_json,
    )


IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
SAFETY_CLASSES = {"advisory", "standard", "high", "critical"}
STOP_WORDS = {"the", "and", "for", "with", "from", "this", "that", "are", "was"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _keywords(text: str) -> List[str]:
    words = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
    return [word for word, _ in Counter(w for w in words if w not in STOP_WORDS).most_common(10)]


def _source_documents() -> List[Dict[str, Any]]:
    documents: List[Dict[str, Any]] = []
    speakers = load_json("speakers.json")
    documents.extend(build_schedule_docs(load_json("schedule.json")))
    documents.extend(build_session_docs(load_json("sessions.json"), speakers))
    documents.extend(build_speaker_docs(speakers))
    documents.extend(build_venue_docs(load_json("venues.json")))
    documents.extend(build_city_docs(load_json("city_guide.json")))
    documents.extend(build_architecture_docs(load_json("architecture.json")))
    return documents


def _input_documents(path: Path) -> List[Dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("documents")
    if not isinstance(value, list):
        raise ValueError("input documents must be a JSON list or a documents list wrapper")
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each input document must be an object")
        normalized.append(
            {
                "doc_id": item.get("doc_id") or item.get("id"),
                "text": item.get("text"),
                "metadata": item.get("metadata") or {},
            }
        )
    return normalized


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_package(args: argparse.Namespace) -> Path:
    if not IDENTIFIER.fullmatch(args.event_id) or not IDENTIFIER.fullmatch(args.version):
        raise ValueError("event ID and version must use lowercase letters, digits, '.', '_' or '-'")
    created_at = args.created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    target = Path(args.output_dir) / args.event_id / args.version
    if target.exists():
        raise FileExistsError("immutable corpus version already exists: {0}".format(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".{0}.staging-".format(args.version), dir=str(target.parent))
    )

    try:
        input_path = Path(args.input_documents) if args.input_documents else None
        source_documents = _input_documents(input_path) if input_path else _source_documents()
        if input_path:
            sources = [{"name": input_path.name, "sha256": _sha256(input_path)}]
        else:
            source_names = (
                "schedule.json", "sessions.json", "speakers.json", "venues.json",
                "city_guide.json", "architecture.json", "treasure_hunt.json",
            )
            sources = [
                {"name": name, "sha256": _sha256(DATA_DIR / name)} for name in source_names
            ]

        records: Dict[str, Dict[str, Any]] = {}
        categories = set()
        index: Dict[str, str] = {}
        for source in source_documents:
            document_id = source.get("doc_id")
            if not isinstance(document_id, str) or not document_id.strip():
                raise ValueError("every document requires a non-empty id or doc_id")
            if document_id in records:
                raise ValueError("duplicate corpus document ID: {0}".format(document_id))
            raw_text = source.get("text")
            if not isinstance(raw_text, str) or not raw_text.strip():
                raise ValueError("document has no text: {0}".format(document_id))
            text = raw_text.strip()
            raw_metadata = source.get("metadata") or {}
            if not isinstance(raw_metadata, dict):
                raise ValueError("document metadata must be an object: {0}".format(document_id))
            metadata = dict(raw_metadata)
            for key, value in metadata.items():
                if not isinstance(key, str) or not key:
                    raise ValueError("metadata keys must be non-empty strings")
                if not isinstance(value, (str, int, float, bool)):
                    raise ValueError(
                        "metadata values must be scalar for Chroma: {0}.{1}".format(
                            document_id, key
                        )
                    )
            if (
                "safety_class" in metadata
                and metadata["safety_class"] not in SAFETY_CLASSES
            ):
                raise ValueError(
                    "document safety_class is invalid: {0}".format(document_id)
                )
            category = metadata.get("category", "general")
            if not isinstance(category, str) or not category:
                raise ValueError("document category must be a non-empty string")
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            records[document_id] = {
                "id": document_id,
                "title": metadata.get("title", ""),
                "text": text,
                "category": category,
                "keywords": _keywords(text),
                "metadata": metadata,
                "content_hash": content_hash,
                "created_at": created_at,
                "updated_at": created_at,
                "word_count": len(text.split()),
                "char_count": len(text),
            }
            categories.add(category)
            index[document_id] = content_hash

        _write_json(staging / "documents.json", records)
        _write_json(staging / "categories.json", {"categories": sorted(categories)})
        _write_json(staging / "document_index.json", index)

        # Event interactions are release content too. Keep them optional for
        # event-neutral input packages, but signed and immutable when present.
        event_asset = DATA_DIR / "treasure_hunt.json"
        if input_path is None and event_asset.is_file():
            shutil.copyfile(event_asset, staging / event_asset.name)

        package_files = {}
        package_names = ["documents.json", "categories.json", "document_index.json"]
        if (staging / "treasure_hunt.json").is_file():
            package_names.append("treasure_hunt.json")
        for name in package_names:
            file_path = staging / name
            package_files[name] = {
                "sha256": _sha256(file_path), "bytes": file_path.stat().st_size
            }
        manifest = {
            "schema_version": "1.0",
            "event": {"id": args.event_id, "name": args.event_name},
            "corpus": {
                "version": args.version,
                "created_at": created_at,
                "document_count": len(records),
            },
            "sources": sources,
            "files": package_files,
        }
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)

        if args.signing_key:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            private_key = serialization.load_pem_private_key(
                Path(args.signing_key).read_bytes(), password=None
            )
            if not isinstance(private_key, Ed25519PrivateKey):
                raise ValueError("corpus signing key must be Ed25519")
            signature = private_key.sign(manifest_path.read_bytes())
            (staging / "manifest.sig").write_text(
                base64.b64encode(signature).decode("ascii") + "\n", encoding="ascii"
            )
        os.replace(str(staging), str(target))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", default="dist/corpora")
    parser.add_argument("--created-at", help="ISO-8601 timestamp for reproducible builds")
    parser.add_argument("--signing-key", help="Ed25519 private key in PEM format")
    parser.add_argument(
        "--input-documents",
        help="event-neutral JSON list of {id or doc_id, text, metadata}; defaults to Summit sources",
    )
    args = parser.parse_args()
    package = build_package(args)
    print("Corpus package created: {0}".format(package))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
