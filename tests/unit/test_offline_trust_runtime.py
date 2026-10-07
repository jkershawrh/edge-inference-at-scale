import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.services.rag_service.offline_trust_runtime import (
    OfflineTrustRuntime,
    OfflineTrustRuntimeError,
)
from backend.services.rag_service.trusted_state import canonical_unsigned


def _sign(value, key):
    value = dict(value)
    value["signature"] = {
        "algorithm": "Ed25519",
        "key_id": value["authority"]["key_id"],
        "value": "",
    }
    value["signature"]["value"] = base64.b64encode(
        key.sign(canonical_unsigned(value))
    ).decode()
    return value


def _write_public(path: Path, key):
    path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))


def _settings(tmp_path, time_key, revocation_key):
    boot = tmp_path / "boot-id"
    boot.write_text("boot-session-001")
    time_public = tmp_path / "time.pem"
    revoke_public = tmp_path / "revoke.pem"
    _write_public(time_public, time_key)
    _write_public(revoke_public, revocation_key)
    scope = {
        "event_id": "flood-response-2026",
        "site_id": "site-alpha",
        "node_id": "node-alpha",
    }
    anchor = _sign({
        "schema_version": "1.0.0", "record_type": "time_anchor",
        "anchor_id": "anchor-field-001", "sequence": 1, "scope": scope,
        "authority": {"key_id": "time-authority-01", "trust_generation": 4},
        "anchored_at": "2026-10-06T18:00:00Z", "max_offline_seconds": 3600,
        "previous_anchor_digest": None,
    }, time_key)
    snapshot = _sign({
        "schema_version": "1.0.0", "record_type": "revocation_snapshot",
        "snapshot_id": "snapshot-field-001", "generation": 1, "scope": scope,
        "authority": {"key_id": "revocation-authority-01", "trust_generation": 4},
        "issued_at": "2026-10-06T18:00:00Z",
        "next_update_at": "2026-10-06T18:30:00Z",
        "previous_snapshot_digest": None, "entries": [],
    }, revocation_key)
    anchor_path, snapshot_path = tmp_path / "anchor.json", tmp_path / "snapshot.json"
    anchor_path.write_text(json.dumps(anchor))
    snapshot_path.write_text(json.dumps(snapshot))
    return SimpleNamespace(
        corpus_event_id="flood-response-2026",
        node_id="node-alpha",
        offline_trust_site_id="site-alpha",
        offline_trust_state_path=str(tmp_path / "state.json"),
        offline_trust_boot_id_path=str(boot),
        offline_trust_generation=4,
        offline_trust_max_snapshot_age_seconds=1800,
        offline_trust_time_key_id="time-authority-01",
        offline_trust_revocation_key_id="revocation-authority-01",
        offline_trust_release_signing_key_id="release-signing-01",
        offline_trust_time_public_key_path=str(time_public),
        offline_trust_revocation_public_key_path=str(revoke_public),
        offline_trust_anchor_path=str(anchor_path),
        offline_trust_revocation_snapshot_path=str(snapshot_path),
    )


def _active():
    return SimpleNamespace(status=SimpleNamespace(digest="sha256:" + "a" * 64))


def test_startup_loads_evidence_and_restart_is_idempotent(tmp_path, monkeypatch):
    time_key, revocation_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    settings = _settings(tmp_path, time_key, revocation_key)
    monkeypatch.setattr("backend.services.rag_service.offline_trust_runtime.time.monotonic", lambda: 100.0)
    runtime = OfflineTrustRuntime.from_settings(settings, _active())
    assert runtime.release_eligibility().eligible is True
    restarted = OfflineTrustRuntime.from_settings(settings, _active())
    assert restarted.health_payload()["offline_trust_eligible"] is True


def test_startup_accepts_projected_volume_symlinks_within_mount(tmp_path, monkeypatch):
    time_key, revocation_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    settings = _settings(tmp_path, time_key, revocation_key)
    original = Path(settings.offline_trust_time_public_key_path)
    projected = tmp_path / "..data"
    projected.mkdir()
    target = projected / "time.pem"
    original.replace(target)
    original.symlink_to(Path("..data") / "time.pem")
    monkeypatch.setattr("backend.services.rag_service.offline_trust_runtime.time.monotonic", lambda: 100.0)
    runtime = OfflineTrustRuntime.from_settings(settings, _active())
    assert runtime.release_eligibility().eligible


def test_startup_rejects_tampered_or_missing_configuration(tmp_path, monkeypatch):
    time_key, revocation_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    settings = _settings(tmp_path, time_key, revocation_key)
    monkeypatch.setattr("backend.services.rag_service.offline_trust_runtime.time.monotonic", lambda: 100.0)
    value = json.loads(Path(settings.offline_trust_anchor_path).read_text())
    value["max_offline_seconds"] = 99_999
    Path(settings.offline_trust_anchor_path).write_text(json.dumps(value))
    with pytest.raises(OfflineTrustRuntimeError, match="rejected"):
        OfflineTrustRuntime.from_settings(settings, _active())
    with pytest.raises(OfflineTrustRuntimeError, match="activation-managed"):
        OfflineTrustRuntime.from_settings(settings, None)


def test_contributing_sources_require_explicit_provenance(tmp_path, monkeypatch):
    time_key, revocation_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    settings = _settings(tmp_path, time_key, revocation_key)
    monkeypatch.setattr("backend.services.rag_service.offline_trust_runtime.time.monotonic", lambda: 100.0)
    runtime = OfflineTrustRuntime.from_settings(settings, _active())
    assert runtime.sources_eligibility([{"source_ids": "source-a,source-b"}]).eligible
    missing = runtime.sources_eligibility([{"parent_doc_id": "doc-a"}])
    assert missing.eligible is False
    assert "SOURCE_PROVENANCE_MISSING" in missing.reason_codes


def test_revoked_contributing_source_and_active_release_are_refused(tmp_path, monkeypatch):
    time_key, revocation_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    settings = _settings(tmp_path, time_key, revocation_key)
    snapshot_path = Path(settings.offline_trust_revocation_snapshot_path)
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["entries"] = [
        {
            "subject_type": "source", "subject_id": "source-revoked",
            "reason_code": "authority-withdrawn",
            "effective_at": "2026-10-06T18:00:00Z",
            "severity": "critical", "action": "stop_serving",
        },
        {
            "subject_type": "release", "subject_id": "sha256:" + "a" * 64,
            "reason_code": "release-withdrawn",
            "effective_at": "2026-10-06T18:00:00Z",
            "severity": "critical", "action": "stop_serving",
        },
    ]
    snapshot["signature"]["value"] = base64.b64encode(
        revocation_key.sign(canonical_unsigned(snapshot))
    ).decode()
    snapshot_path.write_text(json.dumps(snapshot))
    monkeypatch.setattr("backend.services.rag_service.offline_trust_runtime.time.monotonic", lambda: 100.0)
    runtime = OfflineTrustRuntime.from_settings(settings, _active())
    assert runtime.release_eligibility().eligible is False
    decision = runtime.sources_eligibility([{"source_id": "source-revoked"}])
    assert decision.eligible is False
    assert decision.reason_codes == ("SUBJECT_STOPPED_SERVING",)
