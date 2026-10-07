import base64
import copy
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from corpus_factory.evaluation_attestation import authorize_release_signing, bytes_digest, object_digest
from corpus_factory.release_signing import (
    ReleaseSigningError, ReleaseSigningPolicy, ReleaseSigningService,
    SigningKeyMetadata, SigningReplayStore, verify_release_signature,
)
from tests.corpus_factory.test_evaluation_attestation import _attestation

RELEASE_KEY_ID = "release-key-2026-01"


class FakeProtectedAdapter:
    def __init__(self, metadata, key):
        self.metadata, self.__key, self.sign_calls = metadata, key, 0

    def describe_key(self, key_id):
        if key_id != self.metadata.key_id:
            raise ReleaseSigningError("unknown key")
        return self.metadata

    def sign(self, key_id, payload):
        assert key_id == self.metadata.key_id
        self.sign_calls += 1
        return self.__key.sign(payload)


@dataclass
class Context:
    service: ReleaseSigningService
    adapter: FakeProtectedAdapter
    release_key: Ed25519PrivateKey
    candidate: bytes
    authorization: dict
    report: dict
    attestation: dict


@pytest.fixture
def signing(tmp_path):
    candidate, report, attestation, _, attestation_public_key = _attestation(b'{"release":"one"}')
    authorization = authorize_release_signing(
        candidate, report, attestation, attestation_public_key, as_of="2026-10-06T19:00:00Z"
    )
    release_key = Ed25519PrivateKey.generate()
    metadata = SigningKeyMetadata(
        key_id=RELEASE_KEY_ID, role="corpus-release", algorithm="Ed25519",
        generation=3, rotation_state="active", revocation_generation=11,
        valid_from="2026-10-01T00:00:00Z", expires_at="2026-11-01T00:00:00Z",
        public_key_spki=release_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )
    adapter = FakeProtectedAdapter(metadata, release_key)
    policy = ReleaseSigningPolicy(
        allowed_key_ids=(RELEASE_KEY_ID,),
        trusted_attestation_keys=((attestation["signing_key"]["key_id"], attestation_public_key),),
        minimum_key_revocation_generation=11,
        minimum_attestation_revocation_generation=7,
    )
    service = ReleaseSigningService(
        adapter, policy, SigningReplayStore(tmp_path / "replay.db"),
        clock=lambda: datetime(2026, 10, 6, 19, 5, tzinfo=timezone.utc),
    )
    return Context(service, adapter, release_key, candidate, authorization, report, attestation)


def sign(context):
    return context.service.sign(
        context.candidate, context.authorization, context.report, context.attestation,
        key_id=RELEASE_KEY_ID,
    )


def test_signs_exact_authorized_candidate_and_verifies_offline(signing):
    record = sign(signing)
    verify_release_signature(signing.candidate, signing.authorization, record, signing.release_key.public_key())
    assert record["signing_key"]["role"] == "corpus-release"
    assert record["signing_key"]["rotation_state"] == "active"
    assert "public_key_spki" not in json.dumps(record)
    assert not hasattr(signing.adapter, "export_private_key")


def test_exact_replay_is_idempotent_and_changed_request_is_rejected(signing):
    first = sign(signing)
    assert sign(signing) == first
    assert signing.adapter.sign_calls == 2
    signing.adapter.metadata = replace(signing.adapter.metadata, rotation_state="overlap")
    with pytest.raises(ReleaseSigningError, match="different request"):
        sign(signing)


def test_rejects_candidate_or_authorization_tampering(signing):
    with pytest.raises(ReleaseSigningError, match="candidate bytes"):
        signing.service.sign(
            b"changed", signing.authorization, signing.report, signing.attestation,
            key_id=RELEASE_KEY_ID,
        )
    tampered = copy.deepcopy(signing.authorization)
    tampered["authorization"]["authorized_at"] = "2026-10-06T19:01:00Z"
    with pytest.raises(ReleaseSigningError, match="authorization is invalid"):
        signing.service.sign(
            signing.candidate, tampered, signing.report, signing.attestation,
            key_id=RELEASE_KEY_ID,
        )


def test_forged_content_addressed_authorization_cannot_replace_signed_evidence(signing):
    attacker_candidate = b'{"release":"attacker"}'
    forged = copy.deepcopy(signing.authorization)
    forged["candidate_release_digest"] = bytes_digest(attacker_candidate)
    forged["authorization_id"] = object_digest(
        {key: value for key, value in forged.items() if key != "authorization_id"}
    )
    with pytest.raises(ReleaseSigningError, match="covered by signed evaluation evidence"):
        signing.service.sign(
            attacker_candidate, forged, signing.report, signing.attestation,
            key_id=RELEASE_KEY_ID,
        )


def test_tampered_evaluation_signature_fails_closed(signing):
    tampered = copy.deepcopy(signing.attestation)
    tampered["signature"]["value"] = "A" * 88
    with pytest.raises(ReleaseSigningError, match="signed evaluation evidence"):
        signing.service.sign(
            signing.candidate, signing.authorization, signing.report, tampered,
            key_id=RELEASE_KEY_ID,
        )


@pytest.mark.parametrize("state", ["retired", "revoked"])
def test_retired_and_revoked_keys_fail_closed(signing, state):
    signing.adapter.metadata = replace(signing.adapter.metadata, rotation_state=state)
    with pytest.raises(ReleaseSigningError, match="retired or revoked"):
        sign(signing)


def test_overlap_key_is_allowed_but_stale_revocation_is_not(signing):
    signing.adapter.metadata = replace(signing.adapter.metadata, rotation_state="overlap")
    assert sign(signing)["signing_key"]["rotation_state"] == "overlap"
    signing.adapter.metadata = replace(signing.adapter.metadata, revocation_generation=10)
    with pytest.raises(ReleaseSigningError, match="revocation state is stale"):
        sign(signing)


def test_expired_authorization_fails_using_service_owned_clock(signing):
    signing.service._clock = lambda: datetime(2026, 10, 8, 19, 5, tzinfo=timezone.utc)
    with pytest.raises(ReleaseSigningError, match="not current"):
        sign(signing)


def test_signature_tampering_fails_offline_verification(signing):
    record = sign(signing)
    record["signature"]["value"] = "A" * 88
    with pytest.raises(ReleaseSigningError, match="invalid"):
        verify_release_signature(
            signing.candidate, signing.authorization, record, signing.release_key.public_key()
        )


def test_manifest_signature_is_compatible_with_lil_evy_verifier(signing):
    record = sign(signing)
    signature = base64.b64decode(record["manifest_signature"]["value"], validate=True)
    signing.release_key.public_key().verify(signature, signing.candidate)


def _api_payload(signing, candidate=None):
    return {
        "candidate_base64": base64.b64encode(candidate or signing.candidate).decode("ascii"),
        "authorization": signing.authorization,
        "promotion_report": signing.report,
        "evaluation_attestation": signing.attestation,
        "key_id": RELEASE_KEY_ID,
    }


def test_authenticated_api_rejects_missing_auth_and_returns_bounded_evidence(signing):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from corpus_factory.release_signing_api import create_release_signing_app

    client = TestClient(create_release_signing_app(signing.service, bearer_token="t" * 32))
    assert client.post("/v1/sign-release", json=_api_payload(signing)).status_code == 401
    response = client.post(
        "/v1/sign-release", json=_api_payload(signing),
        headers={"Authorization": "Bearer " + "t" * 32},
    )
    assert response.status_code == 200
    assert response.json()["candidate_release_digest"] == bytes_digest(signing.candidate)
    assert "private" not in json.dumps(response.json()).lower()


def test_api_hides_policy_rejection_details(signing):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from corpus_factory.release_signing_api import create_release_signing_app

    client = TestClient(create_release_signing_app(signing.service, bearer_token="t" * 32))
    response = client.post(
        "/v1/sign-release", json=_api_payload(signing, b"wrong"),
        headers={"Authorization": "Bearer " + "t" * 32},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SIGNING_REJECTED"
    assert "digest" not in response.json()["error"]["message"].lower()
