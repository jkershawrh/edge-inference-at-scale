import copy
import json
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from corpus_factory.evaluation_attestation import bytes_digest, object_digest
from corpus_factory.release_signing import (
    ReleaseSigningError,
    ReleaseSigningPolicy,
    ReleaseSigningService,
    SigningKeyMetadata,
    SigningReplayStore,
    verify_release_signature,
)


ATTESTATION_KEY_ID = "a" * 64
RELEASE_KEY_ID = "release-key-2026-01"


class FakeProtectedAdapter:
    def __init__(self, metadata, key):
        self.metadata = metadata
        self.__key = key
        self.sign_calls = 0

    def describe_key(self, key_id):
        if key_id != self.metadata.key_id:
            raise ReleaseSigningError("unknown key")
        return self.metadata

    def sign(self, key_id, payload):
        assert key_id == self.metadata.key_id
        self.sign_calls += 1
        return self.__key.sign(payload)


def authorization(candidate=b'{"release":"one"}'):
    record = {
        "schema_version": "1.0.0",
        "record_type": "release_signing_authorization",
        "candidate_release_digest": bytes_digest(candidate),
        "promotion_report": {"report_id": "sha256:" + "1" * 64, "digest": "sha256:" + "2" * 64},
        "evaluation_attestation": {
            "attestation_id": "sha256:" + "3" * 64,
            "digest": "sha256:" + "4" * 64,
            "key_id": ATTESTATION_KEY_ID,
            "revocation_generation": 7,
        },
        "release_signature_bindings": {
            "evaluation_report_digest": "sha256:" + "2" * 64,
            "approval_attestation_digests": ["sha256:" + "4" * 64],
        },
        "authorization": {
            "action": "sign_exact_candidate_digest",
            "required_key_role": "corpus-release",
            "authorized_at": "2026-10-06T12:00:00Z",
            "private_key_available": False,
        },
    }
    record["authorization_id"] = object_digest(record)
    return record


@pytest.fixture
def signing(tmp_path):
    key = Ed25519PrivateKey.generate()
    public_der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    metadata = SigningKeyMetadata(
        key_id=RELEASE_KEY_ID,
        role="corpus-release",
        algorithm="Ed25519",
        generation=3,
        rotation_state="active",
        revocation_generation=11,
        valid_from="2026-10-01T00:00:00Z",
        expires_at="2026-11-01T00:00:00Z",
        public_key_spki=public_der,
    )
    adapter = FakeProtectedAdapter(metadata, key)
    policy = ReleaseSigningPolicy(
        allowed_key_ids=(RELEASE_KEY_ID,),
        trusted_attestation_key_ids=(ATTESTATION_KEY_ID,),
        minimum_key_revocation_generation=11,
        minimum_attestation_revocation_generation=7,
    )
    service = ReleaseSigningService(adapter, policy, SigningReplayStore(tmp_path / "replay.db"))
    return service, adapter, key


def test_signs_exact_authorized_candidate_and_verifies_offline(signing):
    service, adapter, key = signing
    candidate = b'{"release":"one"}'
    auth = authorization(candidate)
    record = service.sign(candidate, auth, key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")
    verify_release_signature(candidate, auth, record, key.public_key())
    assert record["signing_key"]["role"] == "corpus-release"
    assert record["signing_key"]["rotation_state"] == "active"
    assert "public_key_spki" not in json.dumps(record)
    assert not hasattr(adapter, "export_private_key")


def test_exact_replay_is_idempotent_and_changed_request_is_rejected(signing):
    service, adapter, _ = signing
    candidate = b'{"release":"one"}'
    auth = authorization(candidate)
    first = service.sign(candidate, auth, key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")
    assert service.sign(candidate, auth, key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z") == first
    assert adapter.sign_calls == 2
    with pytest.raises(ReleaseSigningError, match="different request"):
        service.sign(candidate, auth, key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:06:00Z")


def test_rejects_candidate_or_authorization_tampering(signing):
    service, _, _ = signing
    auth = authorization()
    with pytest.raises(ReleaseSigningError, match="candidate bytes"):
        service.sign(b"changed", auth, key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")
    tampered = copy.deepcopy(auth)
    tampered["authorization"]["authorized_at"] = "2026-10-06T12:01:00Z"
    with pytest.raises(ReleaseSigningError, match="authorization is invalid"):
        service.sign(b'{"release":"one"}', tampered, key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")


@pytest.mark.parametrize("state", ["retired", "revoked"])
def test_retired_and_revoked_keys_fail_closed(signing, state):
    service, adapter, _ = signing
    adapter.metadata = replace(adapter.metadata, rotation_state=state)
    with pytest.raises(ReleaseSigningError, match="retired or revoked"):
        service.sign(b'{"release":"one"}', authorization(), key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")


def test_overlap_key_is_allowed_but_stale_revocation_is_not(signing):
    service, adapter, _ = signing
    adapter.metadata = replace(adapter.metadata, rotation_state="overlap")
    record = service.sign(b'{"release":"one"}', authorization(), key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")
    assert record["signing_key"]["rotation_state"] == "overlap"

    service2, adapter2, _ = signing
    adapter2.metadata = replace(adapter2.metadata, revocation_generation=10)
    with pytest.raises(ReleaseSigningError, match="revocation state is stale"):
        service2.sign(b'{"release":"one"}', authorization(), key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")


def test_untrusted_attestation_key_and_expired_authorization_fail(signing):
    service, _, _ = signing
    auth = authorization()
    auth["evaluation_attestation"]["key_id"] = "b" * 64
    auth["authorization_id"] = object_digest({key: value for key, value in auth.items() if key != "authorization_id"})
    with pytest.raises(ReleaseSigningError, match="untrusted attestation"):
        service.sign(b'{"release":"one"}', auth, key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")
    with pytest.raises(ReleaseSigningError, match="not current"):
        service.sign(b'{"release":"one"}', authorization(), key_id=RELEASE_KEY_ID, signed_at="2026-10-08T12:05:00Z")


def test_signature_tampering_fails_offline_verification(signing):
    service, _, key = signing
    candidate = b'{"release":"one"}'
    auth = authorization(candidate)
    record = service.sign(candidate, auth, key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")
    record["signature"]["value"] = "A" * 88
    with pytest.raises(ReleaseSigningError, match="invalid"):
        verify_release_signature(candidate, auth, record, key.public_key())


def test_manifest_signature_is_compatible_with_lil_evy_verifier(signing, tmp_path):
    service, _, key = signing
    candidate = b'{"release":"one"}'
    record = service.sign(candidate, authorization(candidate), key_id=RELEASE_KEY_ID, signed_at="2026-10-06T12:05:00Z")
    signature = __import__("base64").b64decode(record["manifest_signature"]["value"], validate=True)
    key.public_key().verify(signature, candidate)


def test_authenticated_api_rejects_missing_auth_and_returns_bounded_evidence(signing):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from corpus_factory.release_signing_api import create_release_signing_app

    service, _, _ = signing
    app = create_release_signing_app(service, bearer_token="t" * 32)
    client = TestClient(app)
    payload = {
        "candidate_base64": __import__("base64").b64encode(b'{"release":"one"}').decode("ascii"),
        "authorization": authorization(),
        "key_id": RELEASE_KEY_ID,
        "signed_at": "2026-10-06T12:05:00Z",
    }
    assert client.post("/v1/sign-release", json=payload).status_code == 401
    response = client.post(
        "/v1/sign-release",
        json=payload,
        headers={"Authorization": "Bearer " + "t" * 32},
    )
    assert response.status_code == 200
    assert response.json()["candidate_release_digest"] == bytes_digest(b'{"release":"one"}')
    assert "private" not in json.dumps(response.json()).lower()


def test_api_hides_policy_rejection_details(signing):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from corpus_factory.release_signing_api import create_release_signing_app

    service, _, _ = signing
    client = TestClient(create_release_signing_app(service, bearer_token="t" * 32))
    payload = {
        "candidate_base64": __import__("base64").b64encode(b"wrong").decode("ascii"),
        "authorization": authorization(),
        "key_id": RELEASE_KEY_ID,
        "signed_at": "2026-10-06T12:05:00Z",
    }
    response = client.post("/v1/sign-release", json=payload, headers={"Authorization": "Bearer " + "t" * 32})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SIGNING_REJECTED"
    assert "digest" not in response.json()["error"]["message"].lower()
