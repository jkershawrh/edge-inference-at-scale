import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from corpus_factory.distribution import (
    DistributionError,
    REQUIRED_ARTIFACTS,
    export_transfer_set,
    import_transfer_set,
    verify_transfer_set,
)


def _artifacts(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir(parents=True)
    result = {}
    for number, name in enumerate(REQUIRED_ARTIFACTS):
        path = sources / (name + ".blob")
        path.write_bytes((name + ":" + str(number)).encode("utf-8"))
        result[name] = path
    return result


def _export(tmp_path: Path, name="transfer", sequence=7, signer=None):
    return export_transfer_set(
        tmp_path / name,
        _artifacts(tmp_path / ("sources-" + name)),
        media_id="field-media-007",
        event_id="flood-response-2026",
        site_id="site-alpha",
        sequence=sequence,
        classification="restricted",
        manifest_signer=signer,
        allow_unencrypted_lab=True,
    )


def _verify(path: Path, **overrides):
    arguments = {
        "expected_event_id": "flood-response-2026",
        "expected_site_id": "site-alpha",
        "allowed_classifications": {"restricted"},
        "require_signature": False,
    }
    arguments.update(overrides)
    return verify_transfer_set(path, **arguments)


def test_export_is_deterministic_and_verifies_closed_world(tmp_path: Path) -> None:
    first = _export(tmp_path, "one")
    second = _export(tmp_path, "two")

    one = _verify(first)
    two = _verify(second)

    assert one.manifest_digest == two.manifest_digest
    assert [item["name"] for item in one.manifest["artifacts"]] == list(REQUIRED_ARTIFACTS)
    files = {str(path.relative_to(first)) for path in first.rglob("*") if path.is_file()}
    assert "oci-layout" in files and "index.json" in files
    assert all(path.startswith("blobs/sha256/") or path in {"oci-layout", "index.json"}
               for path in files)


@pytest.mark.parametrize("mutation", ["tampered", "partial", "extra"])
def test_tampered_partial_and_extra_blobs_are_rejected(tmp_path: Path, mutation: str) -> None:
    transfer = _export(tmp_path)
    artifact = next(path for path in (transfer / "blobs" / "sha256").iterdir()
                    if path.name != _manifest_hash(transfer))
    if mutation == "tampered":
        artifact.write_bytes(b"tampered")
    elif mutation == "partial":
        artifact.unlink()
    else:
        (transfer / "blobs" / "sha256" / ("f" * 64)).write_bytes(b"extra")

    with pytest.raises(DistributionError):
        _verify(transfer)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"expected_event_id": "other-event"}, "scope"),
        ({"expected_site_id": "site-bravo"}, "scope"),
        ({"allowed_classifications": {"public"}}, "classification"),
        ({"sequence_floor": 7}, "sequence"),
        ({"used_media_ids": ["field-media-007"]}, "already been used"),
    ],
)
def test_scope_policy_rollback_and_media_replay_are_rejected(
    tmp_path: Path, overrides, match: str
) -> None:
    transfer = _export(tmp_path)
    with pytest.raises(DistributionError, match=match):
        _verify(transfer, **overrides)


def test_manifest_path_traversal_is_rejected(tmp_path: Path) -> None:
    transfer = _export(tmp_path)
    manifest = _manifest(transfer)
    manifest["artifacts"][0]["path"] = "../../outside"
    _replace_manifest(transfer, manifest)

    with pytest.raises(DistributionError, match="content-addressed"):
        _verify(transfer)


def test_optional_manifest_signature_verifier_is_enforced(tmp_path: Path) -> None:
    transfer = _export(tmp_path, signer=lambda payload: b"signature:" + payload[:8])

    verified = _verify(
        transfer,
        signature_verifier=lambda payload, signature: signature == b"signature:" + payload[:8],
    )
    assert verified.manifest["media_id"] == "field-media-007"
    with pytest.raises(DistributionError, match="signature"):
        _verify(transfer, signature_verifier=lambda _payload, _signature: False)


def test_import_uses_content_addressed_inbox_and_persists_replay_floor(tmp_path: Path) -> None:
    transfer = _export(tmp_path)
    inbox = tmp_path / "inbox"
    target = import_transfer_set(
        transfer,
        inbox,
        expected_event_id="flood-response-2026",
        expected_site_id="site-alpha",
        allowed_classifications={"restricted"},
        require_signature=False,
    )

    assert target.name == _verify(transfer).manifest_digest.split(":", 1)[1]
    assert (target / "index.json").is_file()
    state = json.loads((inbox / "site-alpha" / "import-state.json").read_text())
    assert state == {"sequence_floor": 7, "used_media_ids": ["field-media-007"]}
    with pytest.raises(DistributionError):
        import_transfer_set(
            transfer,
            inbox,
            expected_event_id="flood-response-2026",
            expected_site_id="site-alpha",
            allowed_classifications={"restricted"},
            require_signature=False,
        )


def test_import_recovers_after_copy_completed_before_state_commit(tmp_path: Path) -> None:
    transfer = _export(tmp_path)
    verified = _verify(transfer)
    inbox = tmp_path / "inbox"
    target = (
        inbox
        / "site-alpha"
        / "transfers"
        / verified.manifest_digest.split(":", 1)[1]
    )
    target.parent.mkdir(parents=True)
    shutil.copytree(transfer, target)

    recovered = import_transfer_set(
        transfer,
        inbox,
        expected_event_id="flood-response-2026",
        expected_site_id="site-alpha",
        allowed_classifications={"restricted"},
        require_signature=False,
    )

    assert recovered == target
    state = json.loads((inbox / "site-alpha" / "import-state.json").read_text())
    assert state["sequence_floor"] == 7
    assert state["used_media_ids"] == ["field-media-007"]


def test_cli_verify_returns_nonzero_for_invalid_transfer(tmp_path: Path) -> None:
    transfer = _export(tmp_path)
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts" / "verify_transfer_set.py"),
        str(transfer),
        "--event-id", "flood-response-2026",
        "--site-id", "site-alpha",
        "--allow-classification", "restricted",
        "--allow-unsigned",
    ]
    success = subprocess.run(command, capture_output=True, text=True)
    assert success.returncode == 0
    (transfer / "unexpected").write_text("not declared", encoding="utf-8")
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0
    assert "verification failed" in failed.stderr


def test_production_verification_requires_trusted_signature_verifier(tmp_path: Path) -> None:
    transfer = _export(tmp_path)
    with pytest.raises(DistributionError, match="trusted transfer signature verifier"):
        verify_transfer_set(
            transfer,
            expected_event_id="flood-response-2026",
            expected_site_id="site-alpha",
            allowed_classifications={"restricted"},
        )


def _manifest_hash(transfer: Path) -> str:
    index = json.loads((transfer / "index.json").read_text())
    return index["manifests"][0]["digest"].split(":", 1)[1]


def _manifest(transfer: Path):
    return json.loads((transfer / "blobs" / "sha256" / _manifest_hash(transfer)).read_text())


def _replace_manifest(transfer: Path, manifest) -> None:
    import hashlib

    old = transfer / "blobs" / "sha256" / _manifest_hash(transfer)
    old.unlink()
    payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    (transfer / "blobs" / "sha256" / digest).write_bytes(payload)
    index = {
        "schemaVersion": 2,
        "manifests": [{
            "mediaType": "application/vnd.evy.transfer.manifest.v1+json",
            "digest": "sha256:" + digest,
            "size": len(payload),
        }],
    }
    (transfer / "index.json").write_text(
        json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n"
    )
