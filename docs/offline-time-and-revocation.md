# Lil EVY offline time and revocation state

Lil EVY now has a software-only, fail-closed contract for carrying time and
revocation evidence into disconnected operation. It implements ADR 0004
without claiming a trusted hardware clock, TPM, GNSS, NTS, or cellular-time
source.

## Signed time anchors

A time-authority Ed25519 signature covers the exact anchor sequence, event,
site and node scope, civil UTC anchor time, maximum offline horizon, previous
anchor digest, authority key ID, and trust-bundle generation. The node accepts
an anchor only when:

- its signature and dedicated time-authority key identity verify;
- event, site, node, and trust generation exactly match local policy;
- its sequence advances the persisted replay floor;
- its previous digest matches the last accepted anchor; and
- it does not move the previously bounded civil time backward.

After acceptance, elapsed time advances only from a non-decreasing monotonic
clock in the same boot epoch. A missing anchor, reboot/epoch change, backward
monotonic value, corrupt state, or elapsed horizon produces `untrusted`, never
an inferred wall-clock value. A newly delivered signed anchor is required after
a reboot because this implementation has no hardware-backed elapsed-time
source.

## Signed revocation snapshots

A separate revocation-authority Ed25519 signature covers a monotonically
increasing generation, exact scope, issue and next-update times, previous
snapshot digest, trust generation, and the complete entry list. Subjects are
explicitly typed as signing keys, exact release digests, sources, or recovery
authorizations. Actions are `block_activation`, `stop_serving`, `restrict`, or
`audit_only`.

The persistent state rejects replay, gaps in the signed digest chain, wrong
scope, wrong authority role, tampering, and future-issued snapshots when
anchored time is available. A caller supplies all contributing source IDs when
checking a candidate or answer, which makes source revocation transitive to
derived documents without relying on text matching.

## Eligibility and health

`OfflineTrustStore.assess_eligibility` returns a machine-readable decision for
activation or serving. It fails closed when time is untrusted, the snapshot is
missing or stale, a matching block/stop action is effective, or a restriction
requires a policy-specific safe subset. It includes only reason codes, time
confidence, effective bounded time, and revocation generation—never queries or
document text.

`OfflineTrustStore.health_payload` exposes the same non-sensitive summary for
service-health composition. Runtime wiring must configure the event policy's
maximum revocation-snapshot age and pass the active release/key/source IDs into
the eligibility check. Until that wiring is configured, the existing runtime
must not claim that revocation-aware serving is enabled.

The state file is written atomically with owner-only permissions. This protects
against crashes and accidental disclosure, not a privileged attacker who can
rewrite the node filesystem. Hardware-backed state sealing remains hardware
validation work.

## Contract shape

Both records use strict, closed field sets, canonical UTC seconds, canonical
JSON for signatures, and detached role-specific Ed25519 verification keys.
Operational private keys are never present on Lil EVY. The executable contract
and validation logic are in
`backend/services/rag_service/trusted_state.py`; golden behavior is covered by
`tests/unit/test_trusted_state.py`.
