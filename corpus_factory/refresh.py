"""Deterministic refresh planning for versioned connected source registries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Sequence

from corpus_factory.acquisition import source_registry_digest, validate_registry_policy_alignment
from corpus_factory.validator import event_policy_subject_digest, validate_instance


class RefreshPlanError(ValueError):
    """Refresh state is ambiguous, malformed, or not bound to the registry."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise RefreshPlanError("{0} must be an ISO-8601 time".format(label))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RefreshPlanError("{0} must be an ISO-8601 time".format(label)) from exc
    if parsed.tzinfo is None:
        raise RefreshPlanError("{0} must include a timezone".format(label))
    return parsed


def plan_registry_refresh(
    registry: Mapping[str, Any],
    event_policy: Mapping[str, Any],
    previous_records: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
) -> Dict[str, Any]:
    """Return a stable due/not-due plan without performing network access."""

    validate_registry_policy_alignment(registry, event_policy)
    as_of_time = _time(as_of, "as_of")
    previous_by_source: Dict[str, Mapping[str, Any]] = {}
    registered_source_ids = {item["source_id"] for item in registry["sources"]}
    for index, record in enumerate(previous_records):
        try:
            validate_instance(record, "source_record")
        except (TypeError, ValueError) as exc:
            raise RefreshPlanError("previous record {0} is invalid: {1}".format(index, exc)) from exc
        source_id = record["source_id"]
        if source_id not in registered_source_ids:
            raise RefreshPlanError("previous source record is not present in the registry")
        if source_id in previous_by_source:
            raise RefreshPlanError("previous source records must be unique")
        previous_by_source[source_id] = record

    entries = []
    for source in sorted(registry["sources"], key=lambda item: item["source_id"]):
        source_id = source["source_id"]
        previous = previous_by_source.get(source_id)
        if not source["enabled"]:
            status = "disabled"
            reason = "connector_disabled"
            due_at = None
            previous_digest = previous["evidence"]["digest"] if previous else None
        elif previous is None:
            status = "due"
            reason = "never_acquired"
            due_at = as_of
            previous_digest = None
        else:
            connector = source["connector"]
            locator = previous["locator"]
            previous_digest = previous["evidence"]["digest"]
            configuration_changed = (
                locator["connector_id"] != connector["connector_id"]
                or locator["acquisition_policy_version"] != registry["acquisition_policy_version"]
                or locator["reference"] != connector["url"]
                or previous["publisher"] != source["publisher"]
                or previous["steward"] != source["steward"]
                or previous["authority_class"] != source["authority_class"]
                or previous["rights"] != source["rights"]
                or previous["scope"] != source["scope"]
                or previous["freshness"] != source["freshness"]
                or previous["sensitivity"] != source["sensitivity"]
                or previous["distribution"] != source["distribution"]
            )
            verified = _time(previous["last_verified_at"], "last_verified_at")
            due_time = verified + timedelta(seconds=source["freshness"]["expected_refresh_seconds"])
            due_at = due_time.isoformat()
            if configuration_changed:
                status = "due"
                reason = "source_configuration_changed"
            elif as_of_time >= due_time:
                status = "due"
                reason = "refresh_interval_elapsed"
            else:
                status = "not_due"
                reason = "refresh_interval_current"
        entries.append({
            "source_id": source_id,
            "connector_id": source["connector"]["connector_id"],
            "status": status,
            "reason": reason,
            "due_at": due_at,
            "previous_digest": previous_digest,
        })

    body = {
        "schema_version": "2.0.0",
        "record_type": "refresh_plan",
        "event_id": registry["event_id"],
        "as_of": as_of,
        "bindings": {
            "source_registry_digest": source_registry_digest(registry),
            "event_policy_subject_digest": event_policy_subject_digest(event_policy),
            "previous_records_digest": _digest(sorted(previous_records, key=lambda item: item["source_id"])),
        },
        "entries": entries,
        "due_source_ids": [entry["source_id"] for entry in entries if entry["status"] == "due"],
    }
    plan = {"plan_id": _digest(body), **body}
    validate_instance(plan, "refresh_plan")
    return plan
