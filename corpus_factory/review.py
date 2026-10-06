"""Deterministic conflict and human-review policy for canonical documents."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from corpus_factory.validator import ContractValidationError, validate_instance


class ReviewPolicyError(ValueError):
    """Raised when records cannot satisfy the configured human-review policy."""


@dataclass(frozen=True)
class ReviewPolicy:
    """Explicit choices for resolution and critical-document separation of duties."""

    allow_authority_resolution: bool = False
    minimum_authority_gap: int = 1
    minimum_critical_approvals: int = 2
    required_critical_roles: Tuple[str, ...] = ("domain_sme", "local_sme")


@dataclass(frozen=True)
class FactConflict:
    fact_id: str
    document_ids: Tuple[str, str]
    state: str
    winner_document_id: Optional[str]


@dataclass(frozen=True)
class ReviewOutcome:
    documents: Tuple[Dict[str, Any], ...]
    conflicts: Tuple[FactConflict, ...]
    blocked_document_ids: Tuple[str, ...]

    @property
    def approval_blocked(self) -> bool:
        return bool(self.blocked_document_ids)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sets_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    return not set(left).isdisjoint(right)


def _effective_windows_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_start = _parse_time(left["valid_from"])
    right_start = _parse_time(right["valid_from"])
    left_end = _parse_time(left["valid_until"])
    right_end = _parse_time(right["valid_until"])
    return (right_end is None or left_start < right_end) and (
        left_end is None or right_start < left_end
    )


def _scopes_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["event_id"] != right["event_id"]:
        return False
    for dimension in ("geographies", "languages", "audiences"):
        if not _sets_overlap(left["scope"][dimension], right["scope"][dimension]):
            return False
    return _effective_windows_overlap(left["validity"], right["validity"])


def _canonical_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _facts_by_id(document: Mapping[str, Any]) -> Dict[str, set[str]]:
    grouped: Dict[str, set[str]] = {}
    for fact in document["structured_facts"]:
        grouped.setdefault(fact["fact_id"], set()).add(_canonical_value(fact["value"]))
    return grouped


def _revision_reference(document: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "document_id": document["document_id"],
        "revision": document["revision"],
        "digest": document["document_digest"],
    }


def _document_key(document: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        document["document_id"],
        document["revision"],
        document["document_digest"],
    )


def _explicitly_supersedes(winner: Mapping[str, Any], loser: Mapping[str, Any]) -> bool:
    expected = _revision_reference(loser)
    return any(reference == expected for reference in winner["conflict"]["supersedes"])


def _authority_winner(
    left: Mapping[str, Any], right: Mapping[str, Any], policy: ReviewPolicy
) -> Optional[Mapping[str, Any]]:
    if not policy.allow_authority_resolution:
        return None
    difference = left["authority_rank"] - right["authority_rank"]
    if abs(difference) < policy.minimum_authority_gap or difference == 0:
        return None
    winner, loser = (left, right) if difference > 0 else (right, left)
    if not _explicitly_supersedes(winner, loser):
        return None
    return winner


def _validate_critical_reviews(
    documents: Sequence[Mapping[str, Any]],
    attestations: Sequence[Mapping[str, Any]],
    policy: ReviewPolicy,
) -> None:
    by_id: Dict[str, Mapping[str, Any]] = {}
    for attestation in attestations:
        try:
            validate_instance(attestation, "review_attestation")
        except ContractValidationError as exc:
            raise ReviewPolicyError("invalid review attestation: {0}".format(exc)) from exc
        attestation_id = attestation["attestation_id"]
        if attestation_id in by_id:
            raise ReviewPolicyError(
                "duplicate review attestation ID: {0}".format(attestation_id)
            )
        by_id[attestation_id] = attestation

    required_roles = set(policy.required_critical_roles)
    if len(required_roles) != len(policy.required_critical_roles):
        raise ReviewPolicyError("critical reviewer roles in policy must be unique")

    for document in documents:
        if document["safety_class"] != "critical":
            continue
        approvals = []
        reference_errors = []
        for attestation_id in document["review_attestation_ids"]:
            attestation = by_id.get(attestation_id)
            if attestation is None:
                reference_errors.append("missing attestation {0}".format(attestation_id))
                continue
            subject = attestation["subject"]
            if (
                subject["type"] != "document"
                or subject["id"] != document["document_id"]
                or subject["digest"] != document["document_digest"]
            ):
                reference_errors.append(
                    "attestation {0} names a different subject".format(attestation_id)
                )
                continue
            if attestation["decision"] == "approve":
                approvals.append(attestation)
        required_count = max(
            [policy.minimum_critical_approvals]
            + [item["critical_approvals_required"] for item in approvals]
        )
        roles = [item["reviewer"]["role"] for item in approvals]
        identities = [item["reviewer"]["identity"] for item in approvals]
        groups = [item["independence_group"] for item in approvals]
        errors = list(reference_errors)
        if len(approvals) < required_count:
            errors.append("needs {0} approvals, found {1}".format(required_count, len(approvals)))
        missing_roles = sorted(required_roles.difference(roles))
        if missing_roles:
            errors.append("missing independent roles: {0}".format(", ".join(missing_roles)))
        if len(roles) != len(set(roles)):
            errors.append("duplicate reviewer roles")
        if len(identities) != len(set(identities)):
            errors.append("duplicate reviewer identities")
        if len(groups) != len(set(groups)):
            errors.append("duplicate independence groups")
        if errors:
            raise ReviewPolicyError(
                "critical document {0}: {1}".format(document["document_id"], "; ".join(errors))
            )


def evaluate_review_policy(
    documents: Sequence[Mapping[str, Any]],
    attestations: Sequence[Mapping[str, Any]],
    policy: ReviewPolicy = ReviewPolicy(),
) -> ReviewOutcome:
    """Detect scoped contradictions and apply explicit review/authority policy.

    Contradictory values are compared only for the same fact identifier and only
    when event, geography, language, audience and effective windows overlap.
    No facts are removed from returned documents, including contested facts.
    """

    if policy.minimum_authority_gap < 1:
        raise ReviewPolicyError("minimum_authority_gap must be at least one")
    if policy.minimum_critical_approvals < 1:
        raise ReviewPolicyError("minimum_critical_approvals must be at least one")

    ordered = sorted(
        (copy.deepcopy(dict(document)) for document in documents),
        key=lambda item: (item["document_id"], item["revision"], item["document_digest"]),
    )
    for document in ordered:
        try:
            validate_instance(document, "canonical_document")
        except ContractValidationError as exc:
            raise ReviewPolicyError("invalid canonical document: {0}".format(exc)) from exc
    _validate_critical_reviews(ordered, attestations, policy)

    states: Dict[tuple[str, int, str], list[tuple[str, str]]] = {}
    conflicts: list[FactConflict] = []
    for left, right in combinations(ordered, 2):
        if not _scopes_overlap(left, right):
            continue
        left_facts = _facts_by_id(left)
        right_facts = _facts_by_id(right)
        for fact_id in sorted(set(left_facts).intersection(right_facts)):
            if left_facts[fact_id] == right_facts[fact_id]:
                continue
            winner = _authority_winner(left, right, policy)
            state = "resolved" if winner is not None else "contested"
            winner_id = winner["document_id"] if winner is not None else None
            pair = tuple(sorted((left["document_id"], right["document_id"])))
            conflicts.append(FactConflict(fact_id, pair, state, winner_id))
            detail = "fact {0} conflicts with {1}".format(fact_id, right["document_id"])
            states.setdefault(_document_key(left), []).append((state, detail))
            detail = "fact {0} conflicts with {1}".format(fact_id, left["document_id"])
            states.setdefault(_document_key(right), []).append((state, detail))

    blocked = []
    for document in ordered:
        document_states = states.get(_document_key(document), [])
        if not document_states:
            continue
        has_contested = any(state == "contested" for state, _ in document_states)
        document["conflict"]["state"] = "contested" if has_contested else "resolved"
        document["conflict"]["explanation"] = "; ".join(
            sorted({detail for _, detail in document_states})
        )
        if has_contested:
            document["approval_state"] = "in_review"
            if document["safety_class"] == "critical":
                blocked.append(document["document_id"])
        validate_instance(document, "canonical_document")

    conflicts.sort(key=lambda item: (item.fact_id, item.document_ids, item.state))
    return ReviewOutcome(tuple(ordered), tuple(conflicts), tuple(sorted(set(blocked))))
