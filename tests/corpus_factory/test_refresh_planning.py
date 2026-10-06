"""Deterministic, policy-bound source refresh planning tests."""

import json
from pathlib import Path

import pytest

from corpus_factory.acquisition import FetchResponse, acquire_registry_source
from corpus_factory.refresh import RefreshPlanError, plan_registry_refresh
from corpus_factory.validator import validate_instance


ROOT = Path(__file__).resolve().parents[2]
VALID = ROOT / "corpus_factory" / "fixtures" / "valid"


def _load(name):
    return json.loads((VALID / name).read_text(encoding="utf-8"))


def _previous(tmp_path, observed_at="2026-10-06T12:00:00Z"):
    body = b'{"shelters":[]}'
    result = acquire_registry_source(
        _load("source-registry.json"),
        _load("event-policy.json"),
        "source-shelter-primary",
        evidence_store=tmp_path / "evidence",
        observed_at=observed_at,
        transport=lambda *args: FetchResponse(
            200,
            "https://alerts.example.gov/region4/shelters.json",
            {"content-type": "application/json", "content-length": str(len(body))},
            body,
        ),
        resolver=lambda host, port: ["8.8.8.8"],
    )
    return result.snapshot.source_record


def _plan(records, as_of, registry=None):
    return plan_registry_refresh(
        registry or _load("source-registry.json"),
        _load("event-policy.json"),
        records,
        as_of=as_of,
    )


def test_never_acquired_source_is_due_and_plan_is_valid():
    plan = _plan([], "2026-10-06T12:00:00Z")
    validate_instance(plan, "refresh_plan")
    assert plan["due_source_ids"] == ["source-shelter-primary"]
    assert plan["entries"][0]["reason"] == "never_acquired"


def test_refresh_interval_transitions_from_current_to_due(tmp_path):
    previous = _previous(tmp_path)
    current = _plan([previous], "2026-10-06T12:14:59Z")
    due = _plan([previous], "2026-10-06T12:15:00Z")
    assert current["entries"][0]["status"] == "not_due"
    assert due["entries"][0]["status"] == "due"
    assert due["entries"][0]["reason"] == "refresh_interval_elapsed"


def test_connector_change_forces_refresh_before_interval(tmp_path):
    previous = _previous(tmp_path)
    registry = _load("source-registry.json")
    registry["sources"][0]["connector"]["url"] = "https://alerts.example.gov/region4/current.json"
    plan = _plan([previous], "2026-10-06T12:01:00Z", registry=registry)
    assert plan["entries"][0]["reason"] == "source_configuration_changed"


def test_governance_change_forces_refresh_before_interval(tmp_path):
    previous = _previous(tmp_path)
    registry = _load("source-registry.json")
    registry["sources"][0]["rights"]["license"] = "Updated public terms"
    plan = _plan([previous], "2026-10-06T12:01:00Z", registry=registry)
    assert plan["entries"][0]["reason"] == "source_configuration_changed"


def test_disabled_source_is_not_due(tmp_path):
    registry = _load("source-registry.json")
    registry["sources"][0]["enabled"] = False
    plan = _plan([], "2026-10-06T12:00:00Z", registry=registry)
    assert plan["due_source_ids"] == []
    assert plan["entries"][0]["status"] == "disabled"


def test_duplicate_previous_records_are_rejected(tmp_path):
    previous = _previous(tmp_path)
    with pytest.raises(RefreshPlanError, match="unique"):
        _plan([previous, previous], "2026-10-06T12:00:00Z")
