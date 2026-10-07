import base64
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.services.rag_service.trusted_state import (
    OfflineTrustStore,
    TrustedStateError,
    canonical_unsigned,
    read_signed_record,
    signed_record_digest,
)


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def _sign(record, key):
    result = copy.deepcopy(record)
    result["signature"] = {
        "algorithm": "Ed25519",
        "key_id": result["authority"]["key_id"],
        "value": "",
    }
    result["signature"]["value"] = base64.b64encode(
        key.sign(canonical_unsigned(result))
    ).decode()
    return result


def _anchor(key, sequence=1, previous=None, anchored_at="2026-10-06T18:00:00Z", horizon=3600):
    return _sign({
        "schema_version": "1.0.0",
        "record_type": "time_anchor",
        "anchor_id": "anchor-001-%d" % sequence,
        "sequence": sequence,
        "scope": {
            "event_id": "flood-response-2026",
            "site_id": "site-alpha",
            "node_id": "node-alpha",
        },
        "authority": {"key_id": "time-authority-01", "trust_generation": 4},
        "anchored_at": anchored_at,
        "max_offline_seconds": horizon,
        "previous_anchor_digest": previous,
    }, key)


def _snapshot(key, generation=1, previous=None, entries=None, issued="2026-10-06T18:00:00Z", next_update="2026-10-06T19:00:00Z"):
    return _sign({
        "schema_version": "1.0.0",
        "record_type": "revocation_snapshot",
        "snapshot_id": "snapshot-001-%d" % generation,
        "generation": generation,
        "scope": {
            "event_id": "flood-response-2026",
            "site_id": "site-alpha",
            "node_id": "node-alpha",
        },
        "authority": {"key_id": "revocation-authority-01", "trust_generation": 4},
        "issued_at": issued,
        "next_update_at": next_update,
        "previous_snapshot_digest": previous,
        "entries": entries or [],
    }, key)


def _entry(subject_type="release", subject_id="sha256:" + "a" * 64, action="stop_serving"):
    return {
        "subject_type": subject_type,
        "subject_id": subject_id,
        "reason_code": "operator-revocation",
        "effective_at": "2026-10-06T18:00:00Z",
        "severity": "critical",
        "action": action,
    }


def _store(tmp_path: Path, clock: Clock, boot="boot-session-001"):
    return OfflineTrustStore(
        tmp_path / "trusted-state.json",
        event_id="flood-response-2026",
        site_id="site-alpha",
        node_id="node-alpha",
        trust_generation=4,
        boot_id=boot,
        monotonic=clock,
    )


def test_signed_anchor_advances_time_using_only_same_boot_monotonic_elapsed(tmp_path):
    key, clock = Ed25519PrivateKey.generate(), Clock()
    store = _store(tmp_path, clock)
    accepted = store.accept_time_anchor(
        _anchor(key), expected_key_id="time-authority-01", public_key=key.public_key()
    )
    assert accepted.state == "anchored"
    clock.value += 45
    status = store.time_confidence()
    assert status.effective_time == datetime(2026, 10, 6, 18, 0, 45, tzinfo=timezone.utc)
    assert status.reason_code == "ANCHOR_WITHIN_OFFLINE_HORIZON"


def test_missing_expired_backward_and_rebooted_monotonic_time_fail_closed(tmp_path):
    key, clock = Ed25519PrivateKey.generate(), Clock()
    store = _store(tmp_path, clock)
    assert store.time_confidence().reason_code == "MISSING_TIME_ANCHOR"
    store.accept_time_anchor(
        _anchor(key, horizon=10), expected_key_id="time-authority-01", public_key=key.public_key()
    )
    clock.value = 99
    assert store.time_confidence().reason_code == "MONOTONIC_CLOCK_MOVED_BACKWARD"
    clock.value = 111
    assert store.time_confidence().reason_code == "TIME_ANCHOR_EXPIRED"
    rebooted = _store(tmp_path, Clock(), boot="boot-session-002")
    assert rebooted.time_confidence().reason_code == "MONOTONIC_EPOCH_CHANGED"


def test_anchor_signature_scope_chain_and_replay_are_strict(tmp_path):
    key, clock = Ed25519PrivateKey.generate(), Clock()
    store = _store(tmp_path, clock)
    first = _anchor(key)
    store.accept_time_anchor(first, expected_key_id="time-authority-01", public_key=key.public_key())
    with pytest.raises(TrustedStateError, match="replay floor"):
        store.accept_time_anchor(first, expected_key_id="time-authority-01", public_key=key.public_key())
    with pytest.raises(TrustedStateError, match="chain"):
        store.accept_time_anchor(_anchor(key, 2), expected_key_id="time-authority-01", public_key=key.public_key())
    wrong_scope = _anchor(key, 2, signed_record_digest(first))
    wrong_scope["scope"]["site_id"] = "site-bravo"
    with pytest.raises(TrustedStateError, match="scope"):
        store.accept_time_anchor(wrong_scope, expected_key_id="time-authority-01", public_key=key.public_key())
    tampered = _anchor(key, 2, signed_record_digest(first))
    tampered["max_offline_seconds"] = 99_999
    with pytest.raises(TrustedStateError, match="verification"):
        store.accept_time_anchor(tampered, expected_key_id="time-authority-01", public_key=key.public_key())
    clock.value += 60
    backwards = _anchor(
        key, 2, signed_record_digest(first), anchored_at="2026-10-06T18:00:30Z"
    )
    with pytest.raises(TrustedStateError, match="backward"):
        store.accept_time_anchor(
            backwards, expected_key_id="time-authority-01", public_key=key.public_key()
        )


def test_revocation_snapshot_blocks_exact_release_and_source_derivations(tmp_path):
    time_key, revoke_key, clock = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate(), Clock()
    store = _store(tmp_path, clock)
    store.accept_time_anchor(
        _anchor(time_key), expected_key_id="time-authority-01", public_key=time_key.public_key()
    )
    release = "sha256:" + "a" * 64
    snapshot = _snapshot(revoke_key, entries=[
        _entry("release", release, "stop_serving"),
        _entry("source", "official-water-feed", "block_activation"),
    ])
    store.accept_revocation_snapshot(
        snapshot,
        expected_key_id="revocation-authority-01",
        public_key=revoke_key.public_key(),
    )
    serving = store.assess_eligibility(
        purpose="serving", subjects={"release": [release]}, max_snapshot_age_seconds=3600
    )
    assert serving.eligible is False
    assert serving.reason_codes == ("SUBJECT_STOPPED_SERVING",)
    activation = store.assess_eligibility(
        purpose="activation",
        subjects={"source": ["official-water-feed"]},
        max_snapshot_age_seconds=3600,
    )
    assert activation.reason_codes == ("SUBJECT_BLOCKS_ACTIVATION",)


def test_stale_snapshot_untrusted_time_and_restrict_are_ineligible(tmp_path):
    time_key, revoke_key, clock = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate(), Clock()
    store = _store(tmp_path, clock)
    store.accept_time_anchor(
        _anchor(time_key, horizon=10_000),
        expected_key_id="time-authority-01", public_key=time_key.public_key(),
    )
    snapshot = _snapshot(revoke_key, entries=[_entry("key", "release-key-01", "restrict")])
    store.accept_revocation_snapshot(
        snapshot, expected_key_id="revocation-authority-01", public_key=revoke_key.public_key()
    )
    restricted = store.assess_eligibility(
        purpose="serving", subjects={"key": ["release-key-01"]}, max_snapshot_age_seconds=3600
    )
    assert not restricted.eligible and restricted.restrictions == ("key:release-key-01",)
    clock.value += 3600
    stale = store.assess_eligibility(
        purpose="serving", subjects={}, max_snapshot_age_seconds=1800
    )
    assert stale.reason_codes == ("REVOCATION_SNAPSHOT_STALE",)
    clock.value = 20_000
    unknown = store.assess_eligibility(
        purpose="serving", subjects={}, max_snapshot_age_seconds=1800
    )
    assert set(unknown.reason_codes) == {"TIME_UNTRUSTED", "REVOCATION_FRESHNESS_UNVERIFIABLE"}


def test_snapshot_generation_chain_and_future_issuance_reject(tmp_path):
    time_key, revoke_key, clock = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate(), Clock()
    store = _store(tmp_path, clock)
    store.accept_time_anchor(
        _anchor(time_key), expected_key_id="time-authority-01", public_key=time_key.public_key()
    )
    first = _snapshot(revoke_key)
    store.accept_revocation_snapshot(
        first, expected_key_id="revocation-authority-01", public_key=revoke_key.public_key()
    )
    with pytest.raises(TrustedStateError, match="replay floor"):
        store.accept_revocation_snapshot(
            first, expected_key_id="revocation-authority-01", public_key=revoke_key.public_key()
        )
    with pytest.raises(TrustedStateError, match="chain"):
        store.accept_revocation_snapshot(
            _snapshot(revoke_key, 2),
            expected_key_id="revocation-authority-01", public_key=revoke_key.public_key(),
        )
    future = _snapshot(
        revoke_key, 2, signed_record_digest(first),
        issued="2026-10-06T18:10:00Z", next_update="2026-10-06T19:10:00Z",
    )
    with pytest.raises(TrustedStateError, match="future-issued"):
        store.accept_revocation_snapshot(
            future, expected_key_id="revocation-authority-01", public_key=revoke_key.public_key()
        )


def test_health_projection_contains_no_signed_records_or_subjects(tmp_path):
    payload = _store(tmp_path, Clock()).health_payload(max_snapshot_age_seconds=3600)
    assert payload == {
        "time_confidence": "untrusted",
        "effective_time": None,
        "revocation_generation": 0,
        "offline_trust_eligible": False,
        "offline_trust_reasons": ["REVOCATION_SNAPSHOT_MISSING", "TIME_UNTRUSTED"],
    }


def test_corrupt_persistent_floor_fails_closed(tmp_path):
    key, clock = Ed25519PrivateKey.generate(), Clock()
    store = _store(tmp_path, clock)
    store.accept_time_anchor(
        _anchor(key), expected_key_id="time-authority-01", public_key=key.public_key()
    )
    state = json.loads(store.state_path.read_text())
    state["time_anchor"]["sequence"] = -1
    store.state_path.write_text(json.dumps(state))
    with pytest.raises(TrustedStateError, match="counters"):
        store.time_confidence()


def test_signed_record_reader_rejects_duplicate_fields_and_symlinks(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"1.0.0","schema_version":"2.0.0"}')
    with pytest.raises(TrustedStateError, match="duplicate"):
        read_signed_record(duplicate)
    link = tmp_path / "link.json"
    link.symlink_to(duplicate)
    with pytest.raises(TrustedStateError, match="unsafe"):
        read_signed_record(link)
