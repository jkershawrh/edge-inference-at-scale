"""TDD contract for the bounded connected acquisition controller."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from corpus_factory.acquisition import FetchResponse
from corpus_factory.acquisition_controller import (
    AcquisitionController,
    AcquisitionControllerError,
    ControllerPaths,
)
from corpus_factory.audit import verify_audit_ledger


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "corpus_factory" / "fixtures" / "valid"
OBSERVED_AT = "2026-10-06T12:00:00Z"


def _registry():
    return json.loads((FIXTURES / "source-registry.json").read_text())


def _policy():
    return json.loads((FIXTURES / "event-policy.json").read_text())


def _paths(tmp_path):
    return ControllerPaths(
        evidence_store=tmp_path / "evidence",
        state_file=tmp_path / "state" / "controller.json",
        audit_ledger=tmp_path / "audit" / "acquisition.jsonl",
        outcome_store=tmp_path / "outcomes",
    )


def _resolver(host, port):
    return ["8.8.8.8"]


def _transport(body=b'{"shelters":[]}'):
    def fetch(url, timeout, maximum):
        return FetchResponse(
            status=200,
            final_url=url,
            headers={
                "content-type": "application/json",
                "content-length": str(len(body)),
            },
            body=body,
        )

    return fetch


def test_reconcile_is_registry_bounded_and_persists_state_and_audit(tmp_path):
    paths = _paths(tmp_path)
    controller = AcquisitionController(
        paths,
        actor="urn:evy:acquisition-controller",
        transport=_transport(),
        resolver=_resolver,
    )

    first = controller.reconcile(_registry(), _policy(), observed_at=OBSERVED_AT)
    second = controller.reconcile(_registry(), _policy(), observed_at=OBSERVED_AT)

    assert first["decision"] == "PASS"
    assert first["outcomes"][0]["change"] == "initial"
    assert second["outcomes"][0]["change"] == "unchanged"
    state = json.loads(paths.state_file.read_text())
    assert state["sources"]["source-shelter-primary"]["current_digest"].startswith("sha256:")
    assert verify_audit_ledger(paths.audit_ledger)["entries"] == 6
    events = [json.loads(line)["event_type"] for line in paths.audit_ledger.read_text().splitlines()]
    assert events == [
        "acquisition_planned",
        "acquisition_completed",
        "source_changed",
        "acquisition_planned",
        "acquisition_completed",
        "source_unchanged",
    ]
    assert len(list(paths.outcome_store.glob("*.json"))) == 2
    durable = [json.loads(path.read_text()) for path in paths.outcome_store.glob("*.json")]
    assert all(item["acquisition_report"]["report_id"] == item["report_id"] for item in durable)
    assert all(item["source_record"]["source_id"] == "source-shelter-primary" for item in durable)


def test_unsupported_authenticated_source_fails_closed_with_durable_outcome(tmp_path):
    registry = _registry()
    registry["sources"][0]["connector"]["auth_secret_ref"] = "secret-region4-api"
    called = False

    def transport(*args):
        nonlocal called
        called = True
        return _transport()(*args)

    paths = _paths(tmp_path)
    report = AcquisitionController(
        paths,
        actor="urn:evy:acquisition-controller",
        transport=transport,
        resolver=_resolver,
    ).reconcile(registry, _policy(), observed_at=OBSERVED_AT)

    assert report["decision"] == "FAIL"
    assert report["failed"] == 1
    assert "external secret provider" in report["outcomes"][0]["error"]
    assert called is False
    assert verify_audit_ledger(paths.audit_ledger)["entries"] == 2
    outcome_path = next(paths.outcome_store.glob("*.json"))
    assert json.loads(outcome_path.read_text())["status"] == "failed"


def test_controller_never_accepts_unregistered_or_unbounded_work(tmp_path):
    called = False

    def transport(*args):
        nonlocal called
        called = True
        return _transport()(*args)

    controller = AcquisitionController(
        _paths(tmp_path),
        actor="urn:evy:acquisition-controller",
        max_sources=1,
        transport=transport,
        resolver=_resolver,
    )
    with pytest.raises(AcquisitionControllerError, match="unregistered"):
        controller.reconcile(
            _registry(), _policy(), observed_at=OBSERVED_AT,
            source_ids=["source-not-in-registry"],
        )

    registry = _registry()
    duplicate = copy.deepcopy(registry["sources"][0])
    duplicate["source_id"] = "source-shelter-secondary"
    duplicate["connector"]["connector_id"] = "connector-shelter-secondary"
    registry["sources"].append(duplicate)
    policy = _policy()
    if "source-shelter-secondary" not in policy["authority_registry"][0]["source_ids"]:
        policy["authority_registry"][0]["source_ids"].append("source-shelter-secondary")
    with pytest.raises(AcquisitionControllerError, match="limit is 1"):
        controller.reconcile(registry, policy, observed_at=OBSERVED_AT)
    assert called is False
    assert not controller.paths.audit_ledger.exists()


def test_candidate_source_is_audited_but_never_fetched(tmp_path):
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
        "rationale": "License approval remains pending.",
    }
    called = False

    def transport(*args):
        nonlocal called
        called = True
        return _transport()(*args)

    paths = _paths(tmp_path)
    report = AcquisitionController(
        paths,
        actor="urn:evy:acquisition-controller",
        transport=transport,
        resolver=_resolver,
    ).reconcile(registry, _policy(), observed_at=OBSERVED_AT)

    assert report["decision"] == "FAIL"
    assert "source_not_approved" in report["outcomes"][0]["error"]
    assert called is False
    assert not paths.evidence_store.exists()
    assert verify_audit_ledger(paths.audit_ledger)["entries"] == 2


def test_controller_rejects_corrupt_or_cross_event_state(tmp_path):
    paths = _paths(tmp_path)
    paths.state_file.parent.mkdir(parents=True)
    paths.state_file.write_text(
        json.dumps({
            "schema_version": "1.0.0",
            "event_id": "another-event",
            "registry_digest": None,
            "sources": {},
        })
    )
    controller = AcquisitionController(
        paths,
        actor="urn:evy:acquisition-controller",
        transport=_transport(),
        resolver=_resolver,
    )
    with pytest.raises(AcquisitionControllerError, match="identity"):
        controller.reconcile(_registry(), _policy(), observed_at=OBSERVED_AT)
