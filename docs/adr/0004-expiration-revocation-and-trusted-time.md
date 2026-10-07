# ADR 0004: Expiration, revocation, and trusted-time behavior

- Status: Proposed for Wave 1 contract freeze
- Date: 2026-10-06
- Depends on: ADRs 0001 and 0003
- Scope: Big EVY release policy and Lil EVY answer eligibility

## Context

Disconnected nodes cannot assume that NTP, cellular time, GPS time, or the
hardware clock is correct. They also cannot discover a new revocation until it
is delivered. Nevertheless, emergency information often has strict validity
windows and must not be served after expiry.

Release expiry, document expiry, key revocation, and release revocation are
different controls and require distinct behavior.

## Decision

### Time confidence is explicit

Lil EVY exposes one of three signed state values:

- `trusted`: time was obtained from an authenticated configured source within
  its maximum age and the local clock has not moved backward;
- `anchored`: a signed Big EVY time anchor was accepted previously and elapsed
  duration is advanced by a non-decreasing local monotonic clock; or
- `untrusted`: neither bound is available, state was lost, the clock moved
  backward beyond tolerance, or the anchor exceeded its allowed offline horizon.

A signed `TimeAnchor` binds the authority key ID, UTC time, issuance sequence,
site/deployment, node or fleet scope, maximum offline horizon, previous anchor
digest, and trust-bundle generation. Lil EVY persists the highest anchor
sequence and rejects replay. NTP without authenticated provenance, file
timestamps, registry timestamps, media labels, SMS timestamps, and operator-set
clocks cannot independently establish `trusted` time.

The wall clock determines civil validity windows only while `trusted` or
`anchored`. Sequence remains the anti-rollback authority.

### Freshness is enforced per fact, then per release

Every canonical document has `valid_from`, `valid_until`, `review_due_at`, and
`stale_action`. Derived chunks inherit these fields and cannot extend them.

- Before `valid_from`, the document is not retrievable.
- At `review_due_at`, it is flagged for review; continued use follows policy.
- At or after `valid_until`, `stale_action=block` removes it from retrieval.
- `stale_action=warn` permits retrieval only with a machine-readable stale flag
  and an explicit user-facing caveat approved by policy.
- `stale_action=allow` is limited to stable reference material and must be
  explicitly approved; it is never the default.

Active hazards, evacuation routes, shelter availability, medical instructions,
operational contacts, and other life-safety facts require `stale_action=block`.
An answer assembled from multiple documents receives the most restrictive
applicable freshness result.

The ReleaseManifest has its own `effective_at`, `expires_at`, and
`max_offline_duration`. A release cannot activate before its effective time or
after expiration. On active-release expiration, safe stable subsets may remain
available only if the signed event policy permits document-level continuation;
otherwise the node enters stale service and refuses corpus answers.

### Untrusted time fails conservatively

When time is `untrusted`:

- time-critical and `stale_action=block` documents are ineligible;
- future-effective content is ineligible;
- recovery authorizations requiring wall-clock validation are ineligible;
- stable content is eligible only when its signed policy explicitly permits
  use under untrusted time and its maximum offline duration can be bounded; and
- the system reports degraded time state in health, responses, and receipts.

The LLM must not fill an information gap created by freshness refusal. It returns
a policy-approved unavailable/stale response and, when safe, directs the user
to local authorities or another configured channel.

### Revocation uses monotonic signed snapshots

Big EVY publishes a signed `RevocationSnapshot` with a monotonically increasing
generation, issuance time, next-update time, previous snapshot digest, and
entries for keys, releases, sources, and recovery authorizations. Each entry has
a stable subject ID/digest, reason code, effective time, severity, and action.

Lil EVY persists the highest accepted generation, rejects older generations,
and applies revocation before activation and at request time. Revocation actions are:

- `block_activation`: candidate cannot activate;
- `stop_serving`: matching active content is immediately made ineligible;
- `restrict`: only the signed safe subset or channels remain eligible; or
- `audit_only`: retain evidence without changing service.

Key revocation controls future signature acceptance and may optionally invalidate
specified releases; it does not ambiguously revoke every historical release by
default. Release revocations name exact digests. Source revocations remove every
document transitively derived from the named evidence/source revision according
to the signed action.

### Disconnected revocation has a bounded limitation

A node cannot act on a revocation it has not received. Each event policy sets
`max_revocation_snapshot_age` by safety class. If the latest snapshot exceeds
that bound under trusted or anchored time, affected content follows the same
conservative block/restrict rules as untrusted time. If time is untrusted, content
requiring fresh revocation status is blocked.

Registry pulls, field provisioning, and physical-media transfers must carry the
newest available trust bundle, revocation snapshot, and time anchor, even when
no new corpus release exists. A compact signed urgent bulletin over constrained
radio may notify or revoke, but its protocol and replay protection require a
separate ADR and implementation before use.

## Consequences

- Lil EVY can explain whether data is current, stale, revoked, or unverifiable.
- Time loss reduces answer coverage instead of silently increasing risk.
- Disconnected operation has an explicit maximum safe horizon for dynamic data.
- Operations must distribute trust, revocation, and time artifacts independently
  of large corpus packages.
- Policies need carefully approved stable subsets so a time failure does not
  unnecessarily remove timeless emergency guidance.

## Required records and tests

- Fixtures cover future, review-due, expired, mixed-freshness, backward-clock,
  missing-anchor, expired-anchor, anchor replay, stale revocation snapshot,
  revoked key, exact release revocation, transitive source revocation, and
  recovery while time is untrusted.
- Every response trace records release digest, contributing document revisions,
  freshness decision, time-confidence state, and revocation generation without
  retaining user message content.
- Field drills include prolonged disconnection beyond every configured freshness
  horizon and restoration with a newer anchor/snapshot.

## Unresolved decisions

- Trusted time sources for each field profile: authenticated network time,
  TPM-secured real-time clock, GNSS, signed provisioning anchors, or combinations.
- Maximum offline and revocation-snapshot ages by safety class and scenario.
- Exact response wording and local-language approval process for stale or
  freshness-unverifiable answers.
- Whether a constrained signed revocation protocol over SMS/LoRa is required in v2.
