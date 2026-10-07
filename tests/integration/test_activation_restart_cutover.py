"""End-to-end Lil EVY activation and workload-restart cutover drill."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from backend.services.rag_service.activation import (
    CorpusActivationManager,
    ReleaseCandidate,
)
from backend.services.rag_service.activation_control import ActivationControl
from backend.services.rag_service.activation_operator import (
    create_activation_operator_app,
)
from backend.services.rag_service.active_release import ActiveReleaseError
from backend.services.rag_service.deployment_reconciliation import (
    DeploymentReconciler,
    ReconciliationState,
)
from backend.services.rag_service.receipt_signing import Ed25519ReceiptSigner
from backend.shared.models import RAGQuery, RAGResult


TOKEN = "field-operator-token-with-at-least-32-bytes"
EVENT_ID = "flood-2026"
CORPUS_VERSION = "field-v1"


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _write_public_key(path: Path, key: Ed25519PrivateKey) -> None:
    path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def _signed_corpus(
    package: Path,
    key: Ed25519PrivateKey,
    *,
    document_id: str,
    guidance: str,
) -> ReleaseCandidate:
    package.mkdir(parents=True)
    content_hash = hashlib.sha256(guidance.encode("utf-8")).hexdigest()
    documents = {
        document_id: {
            "id": document_id,
            "title": "evacuation shelter",
            "text": guidance,
            "category": "emergency",
            "keywords": ["evacuation", "shelter"],
            "metadata": {
                "authority": "county-emergency-office",
                "safety_class": "critical",
            },
            "content_hash": content_hash,
        }
    }
    values = {
        "documents.json": documents,
        "categories.json": {"categories": ["emergency"]},
        "document_index.json": {document_id: content_hash},
    }
    files = {}
    for name, value in values.items():
        path = package / name
        path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        files[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
    manifest = {
        "schema_version": "1.0",
        "event": {"id": EVENT_ID, "name": "Flood Response"},
        "corpus": {"version": CORPUS_VERSION, "document_count": 1},
        "files": files,
    }
    manifest_path = package / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    (package / "manifest.sig").write_text(
        base64.b64encode(key.sign(manifest_path.read_bytes())).decode("ascii"),
        encoding="ascii",
    )
    manifest_digest = "sha256:" + hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    return ReleaseCandidate(manifest_digest, 1, package)


@dataclass
class Drill:
    activation_root: Path
    corpus_public_key: Path
    old: ReleaseCandidate
    new: ReleaseCandidate
    manager: CorpusActivationManager
    operator: TestClient


def _drill(tmp_path: Path) -> Drill:
    corpus_key = Ed25519PrivateKey.generate()
    corpus_public_key = tmp_path / "corpus-public-key.pem"
    _write_public_key(corpus_public_key, corpus_key)
    intake = tmp_path / "intake"
    old = _signed_corpus(
        intake / "old",
        corpus_key,
        document_id="old-shelter",
        guidance="OLD guidance: evacuate to Ridge School.",
    )
    new = _signed_corpus(
        intake / "new",
        corpus_key,
        document_id="new-shelter",
        guidance="NEW guidance: evacuate to North Library.",
    )
    new = ReleaseCandidate(new.digest, 2, new.package_path)

    receipt_key = Ed25519PrivateKey.generate()
    receipt_private_key = tmp_path / "receipt-private-key.pem"
    _write_private_key(receipt_private_key, receipt_key)
    receipt_signer = Ed25519ReceiptSigner(
        receipt_private_key, key_id="lil-evy-001-receipts"
    )
    manager = CorpusActivationManager(
        tmp_path / "activation",
        node_id="lil-evy-001",
        cluster_id="field-cluster-001",
        site_id="flood-site-001",
        trusted_public_key_path=corpus_public_key,
        expected_event_id=EVENT_ID,
        expected_version=CORPUS_VERSION,
        policy_digest=_digest("1"),
        runtime_identity={
            "version": "2.0.0",
            "chunker_digest": _digest("2"),
            "model_digest": _digest("3"),
            "embedding_model_digest": _digest("4"),
        },
        time_confidence="trusted",
        receipt_signer=receipt_signer,
    )
    old_receipt = manager.activate(old)
    unsigned_receipt = old_receipt.to_dict()
    receipt_signature = unsigned_receipt.pop("signature")
    unsigned = json.dumps(
        unsigned_receipt, sort_keys=True, separators=(",", ":")
    ).encode()
    receipt_key.public_key().verify(
        base64.b64decode(receipt_signature["value"]), unsigned
    )

    control = ActivationControl(
        manager, bearer_credential=TOKEN, intake_root=intake
    )
    operator = TestClient(create_activation_operator_app(control))
    return Drill(
        activation_root=tmp_path / "activation",
        corpus_public_key=corpus_public_key,
        old=old,
        new=new,
        manager=manager,
        operator=operator,
    )


def _runtime(drill: Drill, monkeypatch: pytest.MonkeyPatch, cache: Path):
    if "chromadb" not in sys.modules and importlib.util.find_spec("chromadb") is None:
        monkeypatch.setitem(sys.modules, "chromadb", types.ModuleType("chromadb"))
    from backend.shared.config import settings

    monkeypatch.setattr(
        settings, "corpus_activation_root", str(drill.activation_root)
    )
    monkeypatch.setattr(settings, "corpus_event_id", EVENT_ID)
    monkeypatch.setattr(settings, "corpus_version", CORPUS_VERSION)
    monkeypatch.setattr(
        settings, "corpus_public_key_path", str(drill.corpus_public_key)
    )
    monkeypatch.setattr(settings, "corpus_manifest_path", None)
    monkeypatch.setattr(settings, "corpus_require_signature", False)
    monkeypatch.setattr(settings, "corpus_read_only", False)
    monkeypatch.setattr(settings, "embedding_cache_dir", str(cache))
    monkeypatch.setattr(settings, "summit_data_dir", str(cache / "fallback-corpus"))
    from backend.services.rag_service import main as rag_main

    return rag_main.RAGService()


def _accept_new(drill: Drill):
    return drill.operator.post(
        "/v1/activation",
        json={
            "digest": drill.new.digest,
            "sequence": drill.new.sequence,
            "package_path": str(drill.new.package_path),
        },
        headers={"Authorization": "Bearer " + TOKEN},
    )


def _serves_exact(result: RAGResult, candidate: ReleaseCandidate) -> bool:
    return (
        result.active_corpus_digest == candidate.digest
        and result.active_corpus_sequence == candidate.sequence
    )


def _activation_status(runtime) -> dict:
    status = runtime.active_release.status
    return {
        "active_digest": status.digest,
        "active_sequence": status.sequence,
        "sequence_floor": status.sequence_floor,
        "mode": status.mode,
        "state": status.activation_state,
        "ready": True,
        "reason_code": "RECOVERY_ACTIVE" if status.mode == "recovery" else "READY",
    }


@pytest.mark.asyncio
async def test_activation_acceptance_restart_and_exact_attributed_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = _drill(tmp_path)
    old_runtime = _runtime(drill, monkeypatch, tmp_path / "old-cache")
    before = await old_runtime.search(RAGQuery(query="evacuation shelter"))
    assert before.documents == ["OLD guidance: evacuate to Ridge School."]
    assert before.metadata[0]["parent_doc_id"] == "old-shelter"
    assert _serves_exact(before, drill.old)

    accepted = _accept_new(drill)
    assert accepted.status_code == 202
    acknowledgement = accepted.json()
    assert acknowledgement["receipt"]["desired"] == {
        "digest": drill.new.digest,
        "sequence": 2,
    }
    assert acknowledgement["live_reload_performed"] is False
    assert acknowledgement["restart_or_reconciliation_required"] is True
    reconciler = DeploymentReconciler(acknowledgement["receipt"])
    awaiting_restart = reconciler.evaluate(
        _activation_status(old_runtime), restart_attempts=0, elapsed_seconds=1
    )
    assert awaiting_restart.state is ReconciliationState.PENDING

    # Acceptance updates durable desired state, not the already-running RAG
    # process.  Treating the 202 alone as rollout completion would be false.
    still_old = await old_runtime.search(RAGQuery(query="evacuation shelter"))
    assert still_old.documents == ["OLD guidance: evacuate to Ridge School."]
    assert _serves_exact(still_old, drill.old)
    assert not _serves_exact(still_old, drill.new)

    restarted_runtime = _runtime(drill, monkeypatch, tmp_path / "new-cache")
    after = await restarted_runtime.search(RAGQuery(query="evacuation shelter"))

    assert after.documents == ["NEW guidance: evacuate to North Library."]
    assert after.metadata[0]["parent_doc_id"] == "new-shelter"
    assert after.scores[0] > 0.7
    assert _serves_exact(after, drill.new)
    assert restarted_runtime.get_stats()["activation"]["active_digest"] == drill.new.digest
    assert restarted_runtime.get_stats()["activation"]["active_sequence"] == 2
    reconciled = reconciler.evaluate(
        _activation_status(restarted_runtime), restart_attempts=1, elapsed_seconds=2
    )
    assert reconciled.state is ReconciliationState.VERIFIED


@pytest.mark.asyncio
async def test_acceptance_cannot_be_reported_complete_before_workload_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = _drill(tmp_path)
    running_runtime = _runtime(drill, monkeypatch, tmp_path / "running-cache")

    accepted = _accept_new(drill)
    serving = await running_runtime.search(RAGQuery(query="evacuation shelter"))

    assert accepted.status_code == 202
    assert drill.manager.current()["active_digest"] == drill.new.digest
    assert accepted.json()["restart_or_reconciliation_required"] is True
    assert _serves_exact(serving, drill.old)
    assert _serves_exact(serving, drill.new) is False


def test_restart_fails_closed_when_pointer_names_wrong_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drill = _drill(tmp_path)
    assert _accept_new(drill).status_code == 202
    pointer_path = drill.activation_root / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["active_digest"] = drill.old.digest
    pointer_path.write_text(
        json.dumps(pointer, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(ActiveReleaseError, match="sequence|digest"):
        _runtime(drill, monkeypatch, tmp_path / "failed-cache")
