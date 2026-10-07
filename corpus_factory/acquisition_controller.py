"""Bounded, registry-only acquisition reconciliation for connected Big EVY."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from corpus_factory.acquisition import (
    AcquisitionError,
    Resolver,
    Transport,
    _default_resolver,
    acquire_registry_source,
    source_registry_digest,
    urllib_transport,
    validate_registry_policy_alignment,
)
from corpus_factory.audit import append_audit_event


SCHEMA_VERSION = "1.0.0"
MAX_SOURCES_PER_CYCLE = 64


class AcquisitionControllerError(ValueError):
    """Controller inputs or durable state violate the reconciliation contract."""


@dataclass(frozen=True)
class ControllerPaths:
    evidence_store: Path
    state_file: Path
    audit_ledger: Path
    outcome_store: Path


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value).rstrip(b"\n")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _load_state(path: Path, event_id: str) -> Dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "registry_digest": None,
            "sources": {},
        }
    if path.is_symlink() or not path.is_file():
        raise AcquisitionControllerError("controller state must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcquisitionControllerError("controller state is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("event_id") != event_id
        or not isinstance(value.get("sources"), dict)
    ):
        raise AcquisitionControllerError("controller state identity or shape is invalid")
    return value


def _store_outcome(path: Path, outcome: Mapping[str, Any]) -> str:
    digest = _digest(outcome)
    target = path / f"{digest.split(':', 1)[1]}.json"
    data = _canonical_bytes(outcome)
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            raise AcquisitionControllerError("content-addressed outcome collision")
    else:
        _atomic_write(target, data)
    return digest


class AcquisitionController:
    """Reconcile only explicitly registered sources, with a hard per-cycle cap."""

    def __init__(
        self,
        paths: ControllerPaths,
        *,
        actor: str,
        max_sources: int = 32,
        transport: Transport = urllib_transport,
        resolver: Resolver = _default_resolver,
    ) -> None:
        if not isinstance(max_sources, int) or isinstance(max_sources, bool) or not (
            1 <= max_sources <= MAX_SOURCES_PER_CYCLE
        ):
            raise AcquisitionControllerError(
                f"max_sources must be between 1 and {MAX_SOURCES_PER_CYCLE}"
            )
        if not isinstance(actor, str) or len(actor) < 3:
            raise AcquisitionControllerError("actor identity is required")
        self.paths = paths
        self.actor = actor
        self.max_sources = max_sources
        self.transport = transport
        self.resolver = resolver

    def reconcile(
        self,
        registry: Mapping[str, Any],
        event_policy: Mapping[str, Any],
        *,
        observed_at: str,
        source_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Run one bounded pass and return durable per-source outcomes."""

        validate_registry_policy_alignment(registry, event_policy)
        registered = {entry["source_id"]: entry for entry in registry["sources"]}
        if source_ids is None:
            selected = sorted(
                source_id for source_id, entry in registered.items() if entry["enabled"]
            )
        else:
            if not source_ids or len(source_ids) != len(set(source_ids)):
                raise AcquisitionControllerError("source_ids must be non-empty and unique")
            unknown = sorted(set(source_ids) - set(registered))
            if unknown:
                raise AcquisitionControllerError(
                    "controller cannot acquire unregistered source(s): " + ", ".join(unknown)
                )
            selected = sorted(source_ids)
        if len(selected) > self.max_sources:
            raise AcquisitionControllerError(
                f"cycle selected {len(selected)} sources; limit is {self.max_sources}"
            )

        registry_id = source_registry_digest(registry)
        state = _load_state(self.paths.state_file, registry["event_id"])
        outcomes = []
        for source_id in selected:
            planned = {
                "event_id": registry["event_id"],
                "registry_digest": registry_id,
                "source_id": source_id,
                "observed_at": observed_at,
            }
            append_audit_event(
                self.paths.audit_ledger,
                event_type="acquisition_planned",
                occurred_at=observed_at,
                actor=self.actor,
                subject_id=source_id,
                payload_digest=_digest(planned),
            )
            previous = state["sources"].get(source_id)
            previous_digest = (
                previous.get("current_digest") if isinstance(previous, Mapping) else None
            )
            try:
                result = acquire_registry_source(
                    registry,
                    event_policy,
                    source_id,
                    evidence_store=self.paths.evidence_store,
                    observed_at=observed_at,
                    previous_digest=previous_digest,
                    transport=self.transport,
                    resolver=self.resolver,
                )
            except (AcquisitionError, OSError, ValueError) as exc:
                outcome = {
                    "schema_version": SCHEMA_VERSION,
                    "event_id": registry["event_id"],
                    "registry_digest": registry_id,
                    "source_id": source_id,
                    "observed_at": observed_at,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                outcome_digest = _store_outcome(self.paths.outcome_store, outcome)
                append_audit_event(
                    self.paths.audit_ledger,
                    event_type="acquisition_failed",
                    occurred_at=observed_at,
                    actor=self.actor,
                    subject_id=source_id,
                    payload_digest=outcome_digest,
                )
                outcomes.append({**outcome, "outcome_digest": outcome_digest})
                continue

            outcome = {
                "schema_version": SCHEMA_VERSION,
                "event_id": registry["event_id"],
                "registry_digest": registry_id,
                "source_id": source_id,
                "observed_at": observed_at,
                "status": "completed",
                "report_id": result.report["report_id"],
                "current_digest": result.snapshot.digest,
                "change": result.report["change"],
                "acquisition_report": result.report,
                "source_record": result.snapshot.source_record,
            }
            outcome_digest = _store_outcome(self.paths.outcome_store, outcome)
            append_audit_event(
                self.paths.audit_ledger,
                event_type="acquisition_completed",
                occurred_at=observed_at,
                actor=self.actor,
                subject_id=source_id,
                payload_digest=outcome_digest,
            )
            append_audit_event(
                self.paths.audit_ledger,
                event_type=(
                    "source_unchanged"
                    if result.report["change"] == "unchanged"
                    else "source_changed"
                ),
                occurred_at=observed_at,
                actor=self.actor,
                subject_id=source_id,
                payload_digest=result.report["report_id"],
            )
            state["sources"][source_id] = {
                "current_digest": result.snapshot.digest,
                "report_id": result.report["report_id"],
                "observed_at": observed_at,
            }
            outcomes.append({**outcome, "outcome_digest": outcome_digest})

        state["registry_digest"] = registry_id
        _atomic_write(self.paths.state_file, _canonical_bytes(state))
        failed = sum(item["status"] == "failed" for item in outcomes)
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": registry["event_id"],
            "registry_digest": registry_id,
            "observed_at": observed_at,
            "selected": len(selected),
            "completed": len(outcomes) - failed,
            "failed": failed,
            "decision": "PASS" if failed == 0 else "FAIL",
            "outcomes": outcomes,
        }
