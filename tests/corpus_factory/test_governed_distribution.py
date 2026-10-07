import base64
import hashlib
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from backend.services.rag_service.corpus_package import validate_corpus_package
from corpus_factory.distribution import (
    DistributionError,
    EncryptionRecipient,
    export_encrypted_transfer_set,
)
from corpus_factory.evaluation_attestation import authorize_release_signing
from corpus_factory.governed_distribution import (
    GovernedDistributionError,
    GovernedReleaseVerifier,
    import_governed_encrypted_transfer,
    prepare_activation_package,
)
from corpus_factory.release_signing import (
    ReleaseSigningPolicy,
    ReleaseSigningService,
    SigningKeyMetadata,
    SigningReplayStore,
)
from tests.corpus_factory.test_evaluation_attestation import _attestation


class FakeSigner:
    def __init__(self, key, metadata):
        self.key, self.metadata = key, metadata

    def describe_key(self, key_id):
        assert key_id == self.metadata.key_id
        return self.metadata

    def sign(self, key_id, payload):
        assert key_id == self.metadata.key_id
        return self.key.sign(payload)


def _json(path: Path, value) -> Path:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def _package(tmp_path: Path, profile="governed-v1") -> tuple[Path, bytes]:
    package = tmp_path / "package"
    package.mkdir()
    text = "verified emergency guidance"
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    documents = {"guide": {"id": "guide", "text": text, "content_hash": content_hash, "category": "general", "metadata": {}}}
    files = {
        "documents.json": documents,
        "categories.json": {"categories": ["general"]},
        "document_index.json": {"guide": content_hash},
    }
    descriptors = {}
    for name, value in files.items():
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        (package / name).write_bytes(payload)
        descriptors[name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    manifest = {
        "schema_version": "1.0",
        "contract_profile": profile,
        "event": {"id": "flood-response-2026", "name": "Flood response"},
        "corpus": {"version": "release-001", "document_count": 1},
        "files": descriptors,
    }
    candidate = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (package / "manifest.json").write_bytes(candidate)
    archive = tmp_path / "corpus.tar"
    with tarfile.open(archive, "w") as output:
        for path in sorted(package.iterdir()):
            output.add(path, arcname=path.name)
    return archive, candidate


def _fixture(tmp_path: Path):
    corpus, candidate = _package(tmp_path)
    _, report, attestation, _, attestation_public = _attestation(candidate)
    authorization = authorize_release_signing(
        candidate, report, attestation, attestation_public, as_of="2026-10-06T19:00:00Z"
    )
    release_key = Ed25519PrivateKey.generate()
    key_id = "release-key-field-2026"
    metadata = SigningKeyMetadata(
        key_id=key_id, role="corpus-release", algorithm="Ed25519", generation=2,
        rotation_state="active", revocation_generation=5,
        valid_from="2026-10-01T00:00:00Z", expires_at="2027-01-01T00:00:00Z",
        public_key_spki=release_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )
    service = ReleaseSigningService(
        FakeSigner(release_key, metadata),
        ReleaseSigningPolicy(
            allowed_key_ids=(key_id,),
            trusted_attestation_keys=((attestation["signing_key"]["key_id"], attestation_public),),
            minimum_key_revocation_generation=5,
            minimum_attestation_revocation_generation=7,
        ),
        SigningReplayStore(tmp_path / "replay.db"),
        clock=lambda: datetime(2026, 10, 6, 19, 5, tzinfo=timezone.utc),
    )
    signature = service.sign(candidate, authorization, report, attestation, key_id=key_id)
    bundle = _json(tmp_path / "signature-bundle.json", {
        "schema_version": "2.0.0", "contract_profile": "governed-v1",
        "release_signature": signature,
        "release_signing_authorization": authorization,
        "promotion_report": report,
        "evaluation_attestation": attestation,
    })
    verifier = GovernedReleaseVerifier(
        release_key_id=key_id, release_public_key=release_key.public_key(),
        attestation_keys=((attestation["signing_key"]["key_id"], attestation_public),),
        trusted_as_of="2026-10-06T19:05:00Z",
        minimum_attestation_revocation_generation=7,
    )
    artifacts = {
        "application": _json(tmp_path / "application.json", {"name": "lil-evy"}),
        "model": _json(tmp_path / "model.json", {"mode": "rag-only"}),
        "corpus": corpus,
        "signature_bundle": bundle,
        "activation_request": _json(tmp_path / "activation.json", {"event_id": "flood-response-2026"}),
    }
    return artifacts, verifier, release_key


def test_governed_encrypted_import_verifies_before_installing_manifest_signature(tmp_path):
    artifacts, verifier, release_key = _fixture(tmp_path)
    site_key = X25519PrivateKey.generate()
    transfer_key = Ed25519PrivateKey.generate()
    transfer = export_encrypted_transfer_set(
        tmp_path / "transfer", artifacts,
        media_id="field-media-governed-001", event_id="flood-response-2026",
        site_id="site-alpha", sequence=1, classification="restricted",
        recipient=EncryptionRecipient("site-alpha", "site-key-2026", site_key.public_key()),
        manifest_signer=transfer_key.sign, require_governed_release=True,
        governed_release_verifier=verifier,
    )
    imported, decrypted = import_governed_encrypted_transfer(
        transfer, tmp_path / "inbox", tmp_path / "decrypted",
        expected_event_id="flood-response-2026", expected_site_id="site-alpha",
        allowed_classifications={"restricted"},
        signature_verifier=lambda payload, signature: _verify(transfer_key, payload, signature),
        recipient_key_id="site-key-2026", private_key=site_key,
        governed_verifier=verifier,
    )
    assert imported.is_dir()
    assert json.loads((decrypted / "decryption-record.json").read_text())["contract_profile"] == "governed-v1"
    activated = prepare_activation_package(
        decrypted, tmp_path / "activation-package", governed_verifier=verifier
    )
    assert (activated / "manifest.sig").is_file()
    assert (activated / "governance" / "evaluation-attestation.json").is_file()
    validate_corpus_package(
        str(activated / "manifest.json"),
        public_key_path=str(_public_key(tmp_path, release_key)),
        require_signature=True,
    )


def test_governed_export_and_activation_reject_missing_or_tampered_evidence(tmp_path):
    artifacts, verifier, _ = _fixture(tmp_path)
    tampered = json.loads(artifacts["signature_bundle"].read_text())
    del tampered["evaluation_attestation"]
    _json(artifacts["signature_bundle"], tampered)
    with pytest.raises(DistributionError, match="governed release evidence"):
        export_encrypted_transfer_set(
            tmp_path / "transfer", artifacts,
            media_id="field-media-governed-001", event_id="flood-response-2026",
            site_id="site-alpha", sequence=1, classification="restricted",
            recipient=EncryptionRecipient("site-alpha", "site-key-2026", X25519PrivateKey.generate().public_key()),
            require_governed_release=True, governed_release_verifier=verifier,
        )


def test_legacy_activation_requires_explicit_lab_switch(tmp_path):
    corpus, _ = _package(tmp_path, profile="legacy-v1")
    staging = tmp_path / "legacy-staging"
    (staging / "artifacts").mkdir(parents=True)
    (staging / "artifacts" / "corpus").write_bytes(corpus.read_bytes())
    _json(staging / "decryption-record.json", {
        "schema_version": "1.1", "record_type": "decrypted_transfer_staging",
        "source_manifest_digest": "sha256:" + "0" * 64,
        "scope": {"event_id": "flood-response-2026", "site_id": "site-alpha"},
        "sequence": 1, "contract_profile": "legacy-v1",
        "artifacts": [{"name": name} for name in ("application", "model", "corpus", "signature_bundle", "activation_request")],
    })
    with pytest.raises(GovernedDistributionError, match="explicit lab"):
        prepare_activation_package(staging, tmp_path / "blocked")
    activated = prepare_activation_package(staging, tmp_path / "legacy", allow_legacy_lab=True)
    assert (activated / "manifest.json").is_file()
    assert not (activated / "manifest.sig").exists()


def test_tampered_bundle_is_rejected_before_activation_directory_exists(tmp_path):
    artifacts, verifier, _ = _fixture(tmp_path)
    staging = tmp_path / "governed-staging"
    (staging / "artifacts").mkdir(parents=True)
    (staging / "artifacts" / "corpus").write_bytes(artifacts["corpus"].read_bytes())
    bundle = json.loads(artifacts["signature_bundle"].read_text())
    bundle["release_signature"]["manifest_signature"]["value"] = "A" * 88
    _json(staging / "artifacts" / "signature_bundle", bundle)
    _json(staging / "decryption-record.json", {
        "schema_version": "1.1", "record_type": "decrypted_transfer_staging",
        "source_manifest_digest": "sha256:" + "0" * 64,
        "scope": {"event_id": "flood-response-2026", "site_id": "site-alpha"},
        "sequence": 1, "contract_profile": "governed-v1",
        "artifacts": [{"name": name} for name in ("application", "model", "corpus", "signature_bundle", "activation_request")],
    })
    output = tmp_path / "activation-blocked"
    with pytest.raises(GovernedDistributionError, match="signature verification"):
        prepare_activation_package(staging, output, governed_verifier=verifier)
    assert not output.exists()


def _verify(private_key, payload, signature):
    try:
        private_key.public_key().verify(signature, payload)
        return True
    except Exception:
        return False


def _public_key(tmp_path, private_key):
    path = tmp_path / "release-public.pem"
    path.write_bytes(private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    return path
