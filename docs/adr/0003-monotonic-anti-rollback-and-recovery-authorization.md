# ADR 0003: Monotonic anti-rollback and recovery authorization

- Status: Proposed for Wave 1 contract freeze
- Date: 2026-10-06
- Depends on: ADR 0001
- Scope: Lil EVY release selection and recovery

## Context

An older corpus may have a valid signature but contain superseded evacuation
routes, shelter status, medical guidance, or contact details. Wall-clock time is
not sufficient to prevent replay because disconnected nodes may have inaccurate
clocks. At the same time, field operators must be able to recover from a bad
application or index without erasing evidence of the newest release previously
accepted.

## Decision

### Sequence is the primary rollback control

Each event and deployment scope has a strictly increasing logical unsigned
64-bit `release_sequence` allocated by Big EVY. To avoid JSON number precision
differences, it is serialized as a canonical base-10 string with no sign or
leading zeroes. The signed ReleaseManifest also binds the previous release
digest, creating an auditable release chain. Semantic versions are display
labels and do not participate in security comparisons.

Lil EVY persists, per event and deployment scope:

- `sequence_floor`: highest successfully activated production sequence;
- digest associated with that sequence;
- active digest and active mode (`production`, `recovery`, or `golden`);
- highest accepted trust-bundle and revocation generations; and
- a device monotonic counter included in signed receipts.

The state is written transactionally, protected by the strongest available
hardware monotonic or sealed-storage facility, and redundantly journaled with
checksums and generation numbers. Absence or corruption of state enters
`RECOVERY_REQUIRED`; it never resets the floor to zero.

### Ordinary activation can only move forward

- A candidate with sequence greater than the floor may be staged and tested.
- The floor advances only after the new release is active and the service
  acknowledges readiness.
- An equal sequence with the identical digest is an idempotent reinstall or
  repair and does not change the floor.
- An equal sequence with a different digest is always rejected as equivocation.
- A lower sequence is rejected for ordinary activation even when correctly signed.
- A failed candidate never advances the floor or changes the active pointer.

The active pointer and sequence journal update are crash-safe. After power loss,
the node selects either the last acknowledged active release or the newly
acknowledged release; it never exposes a partially built index.

### Routine rollback is pre-authorized recovery, not floor reduction

Every production release may name one `recovery_digest` that was evaluated and
approved with it, normally the immediately preceding known-good release. Lil
EVY may serve that digest in `recovery` mode if activation or runtime health of
the current release fails. The `sequence_floor` remains at the highest activated
sequence, and the node emits a signed recovery receipt and visible degraded state.

Returning from recovery requires a release above the existing floor, or an
idempotent repair of the digest at the floor. Recovery mode never turns an old
release into a new production release.

### Exceptional downgrade needs a separate signed authorization

Any older digest not pre-authorized by the active manifest requires a
`RecoveryAuthorization` signed by a `recovery-authorization` key. It binds:

- authorization UUID and nonce;
- exact target event, deployment/site, and target release digest;
- current minimum sequence floor and currently observed digest;
- reason code, incident reference, issuer, and independent approvers;
- `not_before` and `expires_at`, plus maximum offline-use duration;
- whether serving is allowed and which safety classes must remain blocked; and
- the authorization signature and trust-bundle generation.

Authorizations are single-use per node, recorded in monotonic state, and must
match the node's present floor. They allow temporary `recovery` service but do
not lower or erase the sequence floor. A new production release must use a
sequence higher than the preserved floor.

If trusted time is unavailable, only an authorization explicitly permitting
anchored-time validation may be used, and its offline duration is measured from
the last trusted time anchor using the monotonic clock. An authorization that
cannot be bounded is rejected.

### Golden emergency content is a distinct recovery class

A small, preinstalled golden corpus may contain stable first-aid and system
recovery guidance. It has its own signed identity and `golden_generation`, is
not represented as a low production sequence, and cannot answer event-specific
or time-sensitive questions. Entering golden mode is visible and receipted.

### State repair is controlled

Replacing a failed device, TPM, or anti-rollback journal requires a signed
`StateRecoveryAuthorization` tied to the new node identity, last fleet inventory
record, event/site, expected floor, and incident. It restores the floor; it does
not choose an older value merely because only older media is locally available.

## Consequences

- Replay protection does not depend on a correct wall clock.
- Operational rollback remains possible without normalizing stale content as current.
- A permanently bad high-sequence release does not consume or reset the sequence;
  the next corrected release advances beyond it.
- Loss of monotonic state requires explicit recovery coordination and can delay
  service, which is preferable to silently accepting obsolete life-safety data.
- Big EVY needs a single authoritative sequence allocator per event/scope and
  must detect competing releases at the same sequence before signing.

## Required records and tests

- Tests cover lower sequence, equal sequence/same digest, equal sequence/different
  digest, skipped sequences, failed activation, power loss at every transition,
  pre-authorized recovery, unauthorized recovery, replayed authorization,
  wrong-site authorization, expired authorization, corrupted state, and restored state.
- Activation and recovery receipts report both the floor and the serving digest
  so fleet systems cannot mistake recovery for normal compliance.
- Sequence allocation, failed promotions, and voided sequences remain in the
  immutable Big EVY release ledger.

## Unresolved decisions

- The hardware-backed monotonic storage available on Raspberry Pi, VENTUNO-class,
  and supported x86/Red Hat profiles.
- Whether sequence allocation is global per event or independent per deployment
  scope when two sites legitimately diverge.
- Maximum duration and answer restrictions for recovery and golden modes by
  safety class.
- The quorum and custody model for exceptional recovery authorization.
