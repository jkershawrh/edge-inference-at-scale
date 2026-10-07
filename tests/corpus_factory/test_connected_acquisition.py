"""Connected Big EVY registry and bounded HTTPS acquisition tests."""

import copy
import json
from pathlib import Path

import pytest

from corpus_factory.acquisition import (
    AcquisitionError,
    FetchResponse,
    acquire_registry_source,
    assess_source_acquisition_eligibility,
    source_registry_digest,
    validate_https_url,
)
from corpus_factory.validator import ContractValidationError, validate_instance


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "corpus_factory" / "fixtures" / "valid" / "source-registry.json"
POLICY_FIXTURE = ROOT / "corpus_factory" / "fixtures" / "valid" / "event-policy.json"
OBSERVED_AT = "2026-10-06T12:00:00Z"


def _registry():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _policy():
    return json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))


def _acquire(registry, source_id, **kwargs):
    return acquire_registry_source(registry, _policy(), source_id, **kwargs)


def _resolver(host, port):
    assert port == 443
    return ["203.0.113.20"]


def _global_resolver(host, port):
    return ["8.8.8.8"]


def _response(body=b'{"shelters":[]}', url="https://alerts.example.gov/region4/shelters.json"):
    return FetchResponse(
        200,
        url,
        {"content-type": "application/json; charset=utf-8", "content-length": str(len(body))},
        body,
    )


def test_registry_contract_and_digest_are_deterministic():
    registry = _registry()
    validate_instance(registry, "source_registry")
    assert source_registry_digest(registry) == source_registry_digest(copy.deepcopy(registry))


def test_approved_source_is_acquisition_eligible():
    decision = assess_source_acquisition_eligibility(
        _registry(), _policy(), "source-shelter-primary", observed_at=OBSERVED_AT
    )
    assert decision.eligible is True
    assert decision.reasons == ()


def test_candidate_source_fails_closed_before_network(tmp_path):
    registry = _registry()
    registry["sources"][0]["approval"] = {
        "status": "candidate",
        "checks": {
            "authority_verified": True,
            "geography_verified": True,
            "license_verified": False,
            "validity_verified": True,
            "language_verified": True,
            "risk_classified": True,
        },
        "reviewer_identity": None,
        "reviewed_at": None,
        "rationale": "License evidence is still pending.",
    }
    called = False

    def transport(*args):
        nonlocal called
        called = True
        return _response()

    decision = assess_source_acquisition_eligibility(
        registry, _policy(), "source-shelter-primary", observed_at=OBSERVED_AT
    )
    assert decision.eligible is False
    assert decision.reasons == ("source_not_approved", "license_check_incomplete")
    with pytest.raises(AcquisitionError, match="source_not_approved"):
        _acquire(
            registry, "source-shelter-primary", evidence_store=tmp_path,
            observed_at=OBSERVED_AT, transport=transport, resolver=_global_resolver,
        )
    assert called is False


def test_approved_source_requires_every_classification_check():
    registry = _registry()
    registry["sources"][0]["approval"]["checks"]["geography_verified"] = False
    with pytest.raises(ContractValidationError, match="every classification check"):
        validate_instance(registry, "source_registry")


@pytest.mark.parametrize(
    "observed_at,reason",
    [
        ("2026-10-05T23:59:59Z", "source_not_yet_effective"),
        ("2026-10-07T00:00:00Z", "source_validity_expired"),
    ],
)
def test_source_validity_window_controls_acquisition(observed_at, reason):
    decision = assess_source_acquisition_eligibility(
        _registry(), _policy(), "source-shelter-primary", observed_at=observed_at
    )
    assert decision.eligible is False
    assert decision.reasons == (reason,)


def test_unverified_authority_and_prohibited_rights_cannot_be_approved():
    registry = _registry()
    registry["sources"][0]["authority_class"] = "unverified"
    with pytest.raises(ContractValidationError, match="authority cannot be approved"):
        validate_instance(registry, "source_registry")

    registry = _registry()
    registry["sources"][0]["rights"]["redistribution"] = "prohibited"
    with pytest.raises(ContractValidationError, match="prohibited redistribution"):
        validate_instance(registry, "source_registry")


def test_registry_rejects_duplicate_sources_and_unapproved_host():
    registry = _registry()
    registry["sources"].append(copy.deepcopy(registry["sources"][0]))
    registry["sources"][1]["connector"]["connector_id"] = "connector-second"
    with pytest.raises(ContractValidationError, match="source IDs"):
        validate_instance(registry, "source_registry")

    registry = _registry()
    registry["sources"][0]["connector"]["url"] = "https://unapproved.example.net/data"
    with pytest.raises(ContractValidationError, match="not allowlisted"):
        validate_instance(registry, "source_registry")

    registry = _registry()
    registry["sources"][0]["connector"]["url"] = "https://alerts.example.gov:8443/data"
    with pytest.raises(ContractValidationError, match="port 443"):
        validate_instance(registry, "source_registry")


def test_event_policy_authority_is_required_before_network(tmp_path):
    registry = _registry()
    registry["sources"][0]["publisher"]["id"] = "publisher-unapproved"
    called = False

    def transport(*args):
        nonlocal called
        called = True
        return _response()

    with pytest.raises(AcquisitionError, match="publisher"):
        acquire_registry_source(
            registry,
            _policy(),
            "source-shelter-primary",
            evidence_store=tmp_path,
            observed_at=OBSERVED_AT,
            transport=transport,
            resolver=_global_resolver,
        )
    assert called is False


def test_network_policy_rejects_private_dns_and_literal_ips():
    with pytest.raises(AcquisitionError, match="non-public"):
        validate_https_url(
            "https://alerts.example.gov/data",
            ["alerts.example.gov"],
            resolver=lambda host, port: ["127.0.0.1"],
        )
    with pytest.raises(AcquisitionError, match="literal IP"):
        validate_https_url("https://8.8.8.8/data", ["8.8.8.8"], resolver=_global_resolver)


def test_successful_fetch_is_content_addressed_and_change_report_is_bound(tmp_path):
    registry = _registry()
    result = _acquire(
        registry,
        "source-shelter-primary",
        evidence_store=tmp_path / "evidence",
        observed_at=OBSERVED_AT,
        transport=lambda url, timeout, maximum: _response(),
        resolver=_global_resolver,
    )
    repeated = _acquire(
        registry,
        "source-shelter-primary",
        evidence_store=tmp_path / "evidence",
        observed_at=OBSERVED_AT,
        previous_digest=result.snapshot.digest,
        transport=lambda url, timeout, maximum: _response(),
        resolver=_global_resolver,
    )

    validate_instance(result.snapshot.source_record, "source_record")
    validate_instance(result.report, "acquisition_report")
    assert result.snapshot.path == repeated.snapshot.path
    assert result.snapshot.path.read_bytes() == b'{"shelters":[]}'
    assert result.report["registry_digest"] == source_registry_digest(registry)
    assert result.report["change"] == "initial"
    assert repeated.report["change"] == "unchanged"


def test_changed_payload_creates_new_snapshot(tmp_path):
    registry = _registry()
    first = _acquire(
        registry, "source-shelter-primary", evidence_store=tmp_path / "evidence",
        observed_at=OBSERVED_AT, transport=lambda *args: _response(), resolver=_global_resolver,
    )
    changed_body = b'{"shelters":["north-school"]}'
    second = _acquire(
        registry, "source-shelter-primary", evidence_store=tmp_path / "evidence",
        observed_at=OBSERVED_AT, previous_digest=first.snapshot.digest,
        transport=lambda *args: _response(changed_body), resolver=_global_resolver,
    )
    assert second.report["change"] == "changed"
    assert second.snapshot.digest != first.snapshot.digest
    assert first.snapshot.path.exists()


@pytest.mark.parametrize(
    "response,match",
    [
        (_response(b"x" * 20), "exceeds maximum_bytes"),
        (FetchResponse(200, "https://alerts.example.gov/region4/shelters.json", {"content-type": "text/html", "content-length": "2"}, b"{}"), "media type"),
        (FetchResponse(200, "https://other.example.gov/data", {"content-type": "application/json", "content-length": "2"}, b"{}"), "not allowlisted"),
    ],
)
def test_response_policy_failures_never_write_evidence(tmp_path, response, match):
    registry = _registry()
    registry["sources"][0]["connector"]["maximum_bytes"] = 10
    with pytest.raises(AcquisitionError, match=match):
        _acquire(
            registry, "source-shelter-primary", evidence_store=tmp_path / "evidence",
            observed_at=OBSERVED_AT, transport=lambda *args: response, resolver=_global_resolver,
        )
    assert not (tmp_path / "evidence").exists()


def test_redirects_are_revalidated_and_bounded(tmp_path):
    registry = _registry()
    calls = []

    def transport(url, timeout, maximum):
        calls.append(url)
        if len(calls) == 1:
            return FetchResponse(302, url, {"location": "/region4/current.json"}, b"")
        return _response(url="https://alerts.example.gov/region4/current.json")

    result = _acquire(
        registry, "source-shelter-primary", evidence_store=tmp_path / "evidence",
        observed_at=OBSERVED_AT, transport=transport, resolver=_global_resolver,
    )
    assert result.report["redirect_chain"] == ["https://alerts.example.gov/region4/current.json"]


def test_disabled_and_authenticated_connectors_fail_closed(tmp_path):
    registry = _registry()
    registry["sources"][0]["enabled"] = False
    with pytest.raises(AcquisitionError, match="connector_disabled"):
        _acquire(
            registry, "source-shelter-primary", evidence_store=tmp_path,
            observed_at=OBSERVED_AT, transport=lambda *args: _response(), resolver=_global_resolver,
        )
    registry = _registry()
    registry["sources"][0]["connector"]["auth_secret_ref"] = "secret-region4-api"
    with pytest.raises(AcquisitionError, match="secret provider"):
        _acquire(
            registry, "source-shelter-primary", evidence_store=tmp_path,
            observed_at=OBSERVED_AT, transport=lambda *args: _response(), resolver=_global_resolver,
        )


def test_invalid_time_and_previous_digest_fail_before_network(tmp_path):
    registry = _registry()
    called = False

    def transport(*args):
        nonlocal called
        called = True
        return _response()

    with pytest.raises(AcquisitionError, match="timezone"):
        _acquire(
            registry, "source-shelter-primary", evidence_store=tmp_path,
            observed_at="2026-10-06T12:00:00", transport=transport, resolver=_global_resolver,
        )
    with pytest.raises(AcquisitionError, match="previous_digest"):
        _acquire(
            registry, "source-shelter-primary", evidence_store=tmp_path,
            observed_at=OBSERVED_AT, previous_digest="not-a-digest",
            transport=transport, resolver=_global_resolver,
        )
    assert called is False
