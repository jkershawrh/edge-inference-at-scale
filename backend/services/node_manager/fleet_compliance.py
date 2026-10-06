"""Desired-release compliance policy for the Lil EVY fleet.

Activation acceptance proves that the durable pointer moved.  It does not prove
that a running RAG process serves the release.  This module therefore treats
live, ready production status as the only evidence that can make a node
compliant.  A receipt may only refine a live mismatch to ``pending_restart``.
"""
from __future__ import annotations

from collections import Counter
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from backend.services.rag_service.activation_contracts import (
    ActivationAcceptedResponse,
    ActivationMode,
    ActivationStatusResponse,
    ActivationStatusState,
    Digest,
    ReleaseIdentityResponse,
)


MAX_FLEET_NODES = 10_000


class FleetComplianceError(ValueError):
    """Fleet observations or policy configuration are invalid."""


class FleetComplianceClassification(str, Enum):
    COMPLIANT = "compliant"
    PENDING_RESTART = "pending_restart"
    RECOVERY = "recovery"
    DRIFT = "drift"
    UNREADY = "unready"
    UNKNOWN = "unknown"


class StrictFleetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, strict=True)


class DesiredRelease(StrictFleetModel):
    digest: Digest
    sequence: int = Field(ge=1)


class FleetNodeObservation(StrictFleetModel):
    """Bounded control-plane evidence for one registered node."""

    node_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    online: bool
    activation: Optional[ActivationStatusResponse] = None
    accepted_activation: Optional[ActivationAcceptedResponse] = None


class FleetComplianceNode(StrictFleetModel):
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    classification: FleetComplianceClassification
    observed_digest: Optional[Digest]
    observed_sequence: Optional[int] = Field(default=None, ge=1)


class FleetComplianceCounts(StrictFleetModel):
    compliant: int = Field(ge=0)
    pending_restart: int = Field(ge=0)
    recovery: int = Field(ge=0)
    drift: int = Field(ge=0)
    unready: int = Field(ge=0)
    unknown: int = Field(ge=0)


class FleetComplianceReport(StrictFleetModel):
    """Deterministic, bounded report containing no content, paths, or secrets."""

    desired: DesiredRelease
    fleet_compliant: bool
    total_nodes: int = Field(ge=0, le=MAX_FLEET_NODES)
    counts: FleetComplianceCounts
    nodes: list[FleetComplianceNode] = Field(max_length=MAX_FLEET_NODES)


ObservationInput = Union[FleetNodeObservation, Mapping[str, Any]]


class DesiredReleaseCompliancePolicy:
    """Classify live fleet observations against one immutable release identity."""

    def __init__(self, desired: Union[DesiredRelease, Mapping[str, Any]]) -> None:
        try:
            self.desired = (
                desired
                if isinstance(desired, DesiredRelease)
                else DesiredRelease.model_validate(desired)
            )
        except Exception as exc:
            raise FleetComplianceError("desired release is invalid") from exc

    def evaluate(self, observations: Iterable[ObservationInput]) -> FleetComplianceReport:
        if isinstance(observations, (str, bytes, Mapping)):
            raise FleetComplianceError("fleet observations must be an iterable of nodes")

        validated: list[FleetNodeObservation] = []
        seen: set[str] = set()
        try:
            for value in observations:
                if len(validated) >= MAX_FLEET_NODES:
                    raise FleetComplianceError(
                        f"fleet observations cannot exceed {MAX_FLEET_NODES} nodes"
                    )
                observation = (
                    value
                    if isinstance(value, FleetNodeObservation)
                    else FleetNodeObservation.model_validate(value)
                )
                if observation.node_id in seen:
                    raise FleetComplianceError("fleet observations contain a duplicate node")
                seen.add(observation.node_id)
                validated.append(observation)
        except FleetComplianceError:
            raise
        except Exception as exc:
            raise FleetComplianceError("fleet observation is invalid") from exc

        nodes = sorted(
            (self._classify(observation) for observation in validated),
            key=lambda result: result.node_id,
        )
        counts = Counter(node.classification.value for node in nodes)
        count_model = FleetComplianceCounts(
            **{
                classification.value: counts[classification.value]
                for classification in FleetComplianceClassification
            }
        )
        return FleetComplianceReport(
            desired=self.desired,
            # An empty fleet is not proof that a desired release is deployed.
            fleet_compliant=bool(nodes)
            and counts[FleetComplianceClassification.COMPLIANT.value] == len(nodes),
            total_nodes=len(nodes),
            counts=count_model,
            nodes=nodes,
        )

    def _classify(self, observation: FleetNodeObservation) -> FleetComplianceNode:
        status = observation.activation
        if not observation.online or status is None:
            return self._node(observation, FleetComplianceClassification.UNKNOWN, None)

        if not status.ready:
            return self._node(observation, FleetComplianceClassification.UNREADY, status)

        if (
            status.mode is ActivationMode.RECOVERY
            or status.state is ActivationStatusState.RECOVERY
        ):
            return self._node(observation, FleetComplianceClassification.RECOVERY, status)

        if self._is_live_desired(status):
            return self._node(observation, FleetComplianceClassification.COMPLIANT, status)

        if self._has_exact_acceptance(observation.accepted_activation):
            return self._node(
                observation, FleetComplianceClassification.PENDING_RESTART, status
            )

        return self._node(observation, FleetComplianceClassification.DRIFT, status)

    def _is_live_desired(self, status: ActivationStatusResponse) -> bool:
        return (
            status.active_digest == self.desired.digest
            and status.active_sequence == self.desired.sequence
            and status.mode is ActivationMode.PRODUCTION
            and status.state is ActivationStatusState.ACTIVE
            and status.ready is True
            and status.reason_code == "READY"
        )

    def _has_exact_acceptance(
        self, acceptance: Optional[ActivationAcceptedResponse]
    ) -> bool:
        if acceptance is None:
            return False
        desired = acceptance.receipt.desired
        activated = acceptance.receipt.activated
        return (
            desired == ReleaseIdentityResponse(
                digest=self.desired.digest, sequence=self.desired.sequence
            )
            and activated == desired
            and acceptance.live_reload_performed is False
            and acceptance.restart_or_reconciliation_required is True
        )

    @staticmethod
    def _node(
        observation: FleetNodeObservation,
        classification: FleetComplianceClassification,
        status: Optional[ActivationStatusResponse],
    ) -> FleetComplianceNode:
        sequence = None
        if status is not None and status.active_sequence > 0:
            sequence = status.active_sequence
        return FleetComplianceNode(
            node_id=observation.node_id,
            classification=classification,
            observed_digest=status.active_digest if status is not None else None,
            observed_sequence=sequence,
        )
