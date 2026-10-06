"""Tests for the fail-closed Lil EVY active-release read boundary."""
import base64
import hashlib
import importlib.util
import json
import sys
import types
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.services.rag_service.active_release import (
    ActiveReleaseError,
    ActiveReleaseResolver,
)


def _digest(character: str = "a") -> str:
    return "sha256:" + character * 64


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _signed_package(package: Path, private_key: Ed25519PrivateKey) -> None:
    package.mkdir(parents=True)
    text = "The verified shelter is at the regional school."
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    documents = {
        "shelter": {
            "id": "shelter",
            "text": text,
            "category": "shelter",
            "content_hash": content_hash,
        }
    }
    values = {
        "documents.json": documents,
        "categories.json": {"categories": ["shelter"]},
        "document_index.json": {"shelter": content_hash},
    }
    files = {}
    for name, value in values.items():
        path = package / name
        path.write_text(json.dumps(value), encoding="utf-8")
        files[name] = {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    manifest = {
        "schema_version": "1.0",
        "event": {"id": "flood-2026", "name": "Flood Response"},
        "corpus": {"version": "1.0.0", "document_count": 1},
        "files": files,
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    signature = private_key.sign(manifest_path.read_bytes())
    (package / "manifest.sig").write_text(base64.b64encode(signature).decode(), encoding="ascii")


def _deployment(tmp_path: Path, *, mode: str = "production"):
    root = tmp_path / "activation"
    sequence = 4 if mode == "production" else 2
    floor = 4
    key = Ed25519PrivateKey.generate()
    public_key = tmp_path / "corpus-public-key.pem"
    public_key.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    staging_package = tmp_path / "staging-package"
    _signed_package(staging_package, key)
    digest = "sha256:" + hashlib.sha256(
        (staging_package / "manifest.json").read_bytes()
    ).hexdigest()
    release = root / "releases" / digest.split(":", 1)[1]
    release.mkdir(parents=True, exist_ok=True)
    staging_package.rename(release / "package")
    authorizations = ["recovery-authorization-001"] if mode == "recovery" else []
    _write_json(
        root / "current.json",
        {
            "active_digest": digest,
            "active_sequence": sequence,
            "sequence_floor": floor,
            "mode": mode,
            "device_counter": 8,
            "used_recovery_authorizations": authorizations,
        },
    )
    state = "ACTIVE" if mode == "production" else "RECOVERY"
    _write_json(
        release / "state.json",
        {
            "digest": digest,
            "sequence": sequence,
            "state": state,
            "history": ["STAGED", "VERIFIED", "INDEXED", "CANARY_TESTED", "READY", state],
        },
    )
    resolver = ActiveReleaseResolver(
        root,
        expected_event_id="flood-2026",
        expected_version="1.0.0",
        trusted_public_key_path=public_key,
    )
    return resolver, root, release


def _rag_main_without_optional_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    if importlib.util.find_spec("chromadb") is None:
        monkeypatch.setitem(sys.modules, "chromadb", types.ModuleType("chromadb"))
    from backend.shared.config import settings

    monkeypatch.setattr(settings, "embedding_cache_dir", str(tmp_path / "default-cache"))
    monkeypatch.setattr(settings, "summit_data_dir", str(tmp_path / "default-corpus"))
    monkeypatch.setattr(settings, "corpus_manifest_path", None)
    monkeypatch.setattr(settings, "corpus_activation_root", None)
    monkeypatch.setattr(settings, "corpus_require_signature", False)
    monkeypatch.setattr(settings, "corpus_read_only", False)
    from backend.services.rag_service import main as rag_main

    return rag_main


@pytest.mark.parametrize(
    "mode,expected_state,sequence,floor",
    [("production", "ACTIVE", 4, 4), ("recovery", "RECOVERY", 2, 4)],
)
def test_resolves_only_valid_active_or_recovery_release(
    tmp_path: Path, mode: str, expected_state: str, sequence: int, floor: int
) -> None:
    resolver, _root, release = _deployment(tmp_path, mode=mode)

    selection = resolver.resolve()

    assert selection.package_dir == (release / "package").resolve()
    assert selection.event_id == "flood-2026"
    assert selection.corpus_version == "1.0.0"
    assert selection.document_count == 1
    assert selection.status.digest == json.loads(
        (_root / "current.json").read_text(encoding="utf-8")
    )["active_digest"]
    assert selection.status.sequence == sequence
    assert selection.status.sequence_floor == floor
    assert selection.status.activation_state == expected_state
    with pytest.raises(FrozenInstanceError):
        selection.status.sequence = 99  # type: ignore[misc]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(active_digest="sha256:../escape"),
        lambda value: value.update(active_sequence=True),
        lambda value: value.update(active_sequence=5),
        lambda value: value.update(sequence_floor=0),
        lambda value: value.update(mode="staged"),
        lambda value: value.update(mode=[]),
        lambda value: value.update(device_counter=False),
        lambda value: value.update(used_recovery_authorizations="not-a-list"),
    ],
)
def test_rejects_malformed_or_inconsistent_pointer(tmp_path: Path, mutation) -> None:
    resolver, root, _release = _deployment(tmp_path)
    pointer = json.loads((root / "current.json").read_text())
    mutation(pointer)
    _write_json(root / "current.json", pointer)

    with pytest.raises(ActiveReleaseError):
        resolver.resolve()


@pytest.mark.parametrize(
    "field,value",
    [
        ("digest", _digest("b")),
        ("sequence", 3),
        ("state", "REJECTED"),
        ("state", []),
        ("history", ["STAGED", "READY"]),
    ],
)
def test_rejects_state_that_disagrees_with_pointer(
    tmp_path: Path, field: str, value
) -> None:
    resolver, _root, release = _deployment(tmp_path)
    state = json.loads((release / "state.json").read_text())
    state[field] = value
    _write_json(release / "state.json", state)

    with pytest.raises(ActiveReleaseError):
        resolver.resolve()


def test_rejects_torn_pointer_before_final_active_state(tmp_path: Path) -> None:
    resolver, _root, release = _deployment(tmp_path)
    state = json.loads((release / "state.json").read_text())
    state["state"] = "READY"
    state["history"] = state["history"][:-1]
    _write_json(release / "state.json", state)

    with pytest.raises(ActiveReleaseError, match="not in"):
        resolver.resolve()


@pytest.mark.parametrize("tamper", ["document", "signature", "event"])
def test_revalidates_package_identity_integrity_and_signature(
    tmp_path: Path, tamper: str
) -> None:
    resolver, _root, release = _deployment(tmp_path)
    package = release / "package"
    if tamper == "document":
        (package / "documents.json").write_text("{}", encoding="utf-8")
    elif tamper == "signature":
        (package / "manifest.sig").write_text(base64.b64encode(b"x" * 64).decode())
    else:
        resolver.expected_event_id = "other-event"

    with pytest.raises(ActiveReleaseError, match="package validation"):
        resolver.resolve()


def test_rejects_release_directory_symlink_escape(tmp_path: Path) -> None:
    resolver, root, release = _deployment(tmp_path)
    outside = tmp_path / "outside-release"
    release.rename(outside)
    release.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ActiveReleaseError, match="escapes activation root"):
        resolver.resolve()


def test_rejects_release_store_symlink_even_when_target_stays_inside_root(
    tmp_path: Path,
) -> None:
    resolver, root, _release = _deployment(tmp_path)
    releases = root / "releases"
    relocated = root / "relocated-releases"
    releases.rename(relocated)
    releases.symlink_to(relocated, target_is_directory=True)

    with pytest.raises(ActiveReleaseError, match="release store"):
        resolver.resolve()


@pytest.mark.parametrize("missing", ["pointer", "state", "manifest"])
def test_missing_activation_material_fails_closed(tmp_path: Path, missing: str) -> None:
    resolver, root, release = _deployment(tmp_path)
    path = {
        "pointer": root / "current.json",
        "state": release / "state.json",
        "manifest": release / "package" / "manifest.json",
    }[missing]
    path.unlink()

    with pytest.raises(ActiveReleaseError):
        resolver.resolve()


@pytest.mark.asyncio
async def test_rag_runtime_loads_only_the_verified_active_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver, root, release = _deployment(tmp_path)

    rag_main = _rag_main_without_optional_database(monkeypatch, tmp_path)

    monkeypatch.setattr(rag_main.settings, "corpus_activation_root", str(root))
    monkeypatch.setattr(rag_main.settings, "corpus_event_id", "flood-2026")
    monkeypatch.setattr(rag_main.settings, "corpus_version", "1.0.0")
    monkeypatch.setattr(
        rag_main.settings,
        "corpus_public_key_path",
        str(resolver.trusted_public_key_path),
    )
    monkeypatch.setattr(rag_main.settings, "corpus_manifest_path", None)
    monkeypatch.setattr(rag_main.settings, "corpus_require_signature", False)
    monkeypatch.setattr(rag_main.settings, "corpus_read_only", False)
    monkeypatch.setattr(
        rag_main.settings, "embedding_cache_dir", str(tmp_path / "embedding-cache")
    )

    service = rag_main.RAGService()
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))

    assert service.document_manager.data_dir == (release / "package").resolve()
    assert service.corpus_read_only is True
    assert service.active_release is not None
    assert service.active_release.status.digest == pointer["active_digest"]
    assert set(service.document_manager.documents) == {"shelter"}
    attributed = service._attribute_search_result(
        rag_main.RAGResult(
            documents=["verified guidance"], scores=[0.99], metadata=[{}]
        )
    )
    assert attributed.active_corpus_digest == pointer["active_digest"]
    assert attributed.active_corpus_sequence == 4
    assert service.get_stats()["activation"] == {
        "active_digest": pointer["active_digest"],
        "active_sequence": 4,
        "sequence_floor": 4,
        "mode": "production",
        "state": "ACTIVE",
        "ready": True,
        "reason_code": "READY",
    }

    monkeypatch.setattr(rag_main, "rag_service", service)
    status = await rag_main.get_activation_status()
    assert status.active_digest == pointer["active_digest"]
    assert status.ready is True
    with pytest.raises(rag_main.HTTPException) as blocked:
        await rag_main.add_document(
            rag_main.RAGAddDocumentRequest(text="unreviewed field guidance")
        )
    assert blocked.value.status_code == 403


def test_rag_runtime_fails_closed_when_active_manifest_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver, root, release = _deployment(tmp_path)
    manifest = release / "package" / "manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    rag_main = _rag_main_without_optional_database(monkeypatch, tmp_path)

    monkeypatch.setattr(rag_main.settings, "corpus_activation_root", str(root))
    monkeypatch.setattr(rag_main.settings, "corpus_event_id", "flood-2026")
    monkeypatch.setattr(rag_main.settings, "corpus_version", "1.0.0")
    monkeypatch.setattr(
        rag_main.settings,
        "corpus_public_key_path",
        str(resolver.trusted_public_key_path),
    )
    monkeypatch.setattr(
        rag_main.settings, "embedding_cache_dir", str(tmp_path / "embedding-cache")
    )

    with pytest.raises(ActiveReleaseError, match="digest"):
        rag_main.RAGService()
