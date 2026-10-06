# ADR 0001: Corpus trust model and key roles

- Status: Proposed for Wave 1 contract freeze
- Date: 2026-10-06
- Owners: Big EVY release authority and Lil EVY runtime maintainers
- Scope: Corpus Release v2

## Context

Big EVY builds corpus releases while connected to authoritative sources. Lil EVY
may receive those releases through a registry mirror, an intermittent link, or
physical media and may remain disconnected afterward. Transport security alone
therefore cannot establish whether a release is authorized for a particular
event, site, geography, audience, or time window.

A valid signature proves control of a key. It does not prove that the source is
authoritative, the facts are correct, the release is current, or its data is
safe to disclose. Those properties require signed scope, provenance, review,
evaluation, freshness, and local policy checks.

The principal threats are:

- substitution or modification of release bytes in a registry or on media;
- publication by a valid key for the wrong role, event, site, or data class;
- replay of an older valid release or trust bundle;
- compromise or misuse of an online release-signing key;
- loss, theft, or cloning of transfer media or a Lil EVY node;
- a malicious or mistaken operator bypassing review or evaluation gates;
- an agent treating extracted or generated content as approved fact;
- use of expired or revoked information while a node is disconnected; and
- forged activation receipts that hide fleet drift.

Compromise of a fully unlocked Lil EVY node is outside the trust boundary for
confidentiality of content already activated on that node. The design limits
the blast radius but cannot make plaintext unavailable to a runtime that must
serve it.

## Decision

### Trust is rooted locally

Every Lil EVY deployment is provisioned with an offline-root public key and a
site identity before field deployment. A release is trusted only when all of
the following hold:

1. its signature chains through a non-revoked role key to the provisioned root;
2. the signing key has the `corpus-release` role and was valid when the release
   was signed;
3. the signed manifest binds the exact OCI digest, release sequence, event,
   site/deployment scope, geography, language, audience, classification,
   validity interval, policy digest, approval attestation, and evaluation report;
4. every referenced blob matches its digest and size;
5. the release passes local compatibility, capacity, freshness, revocation,
   anti-rollback, indexing, and smoke-test policy; and
6. activation completes atomically.

Registry tags, TLS sessions, filenames, removable-media labels, Git commits,
and operator identity are discovery or transport signals only. None can grant
activation authority.

### Keys have narrow, non-interchangeable roles

The trust bundle defines each key's role, scope, validity interval, status, and
issuer. Lil EVY rejects a valid signature made by a key with the wrong role.

| Key role | Custody | Permitted operation |
| --- | --- | --- |
| `offline-root` | Offline, dual-controlled HSM or equivalent | Sign trust-bundle generations and role-key certificates only |
| `corpus-release` | Protected signing service/HSM | Sign an exact policy-passing Corpus Release digest |
| `recovery-authorization` | Offline or separately protected, dual-controlled | Authorize exceptional site-scoped recovery under ADR 0003 |
| `revocation` | Highly available protected service plus offline export path | Sign trust/release revocation snapshots |
| `time-authority` | Protected connected service | Sign time anchors for disconnected freshness checks |
| `site-encryption` | Site-held, preferably TPM-bound private key | Unwrap confidential corpus data keys for one site/deployment |
| `device-receipt` | Node-held, preferably TPM-bound private key | Sign activation receipts and monotonic device state |

One key pair cannot hold both `corpus-release` and
`recovery-authorization`. Root keys never sign releases, content, receipts, or
time statements. Release-signing keys never issue other keys.

### The trust bundle is versioned data

The offline root signs a canonical trust bundle containing:

- a monotonically increasing `trust_bundle_generation`;
- root identifier and permitted signature algorithms;
- role-key public keys, stable key IDs, scopes, and validity intervals;
- key and release revocations with reason codes;
- accepted schema and policy authorities; and
- the preceding bundle digest, except for generation one.

Lil EVY persists the highest accepted trust-bundle generation. It accepts an
equal generation only when the digest is identical and never accepts a lower
generation through ordinary installation. Trust-bundle updates are verified
before release verification and are auditable independently from activation.

The initial v2 cryptographic baseline is Ed25519 for root, role, authorization,
time, and receipt signatures. Key IDs are the lowercase hex SHA-256 digest of
the DER SubjectPublicKeyInfo bytes. Algorithms are explicit fields; algorithm
substitution or an unknown critical algorithm fails closed.

Planned root rotation uses a transition bundle signed by both the currently
trusted root and the successor root. Lil EVY accepts it only at a higher bundle
generation and persists the successor as the new root. If the current root is
compromised, no remotely delivered artifact can safely repair that trust;
affected nodes require authenticated reprovisioning under a documented physical
recovery ceremony.

### Authority over facts remains separate from cryptographic authority

The release signature covers approval and evaluation attestations by digest.
Lil EVY verifies that required attestations exist and match policy, but it does
not infer factual authority from the release signer. Source authority and human
review are governed by ADR 0005 and the signed event policy.

Agents and build workers receive no root, release, recovery, revocation, site,
or device private keys. They may prepare a candidate and submit its digest to
the protected signer only after policy gates succeed.

### Verification is offline and fail-closed

The release carries the complete non-secret verification material needed for
its chain, while the root public key and local policy remain independently
provisioned. Missing chains, ambiguous scopes, unknown critical fields,
unrecognized roles, unsupported algorithms, or mismatched digests reject the
candidate without changing the active release.

## Consequences

- The same verification state machine works for connected and air-gapped delivery.
- A registry or media compromise cannot authorize modified content.
- A release-key compromise is constrained by key scope and can be revoked
  without replacing the offline root.
- Site encryption and device receipts can be introduced without conflating
  them with release authenticity.
- Provisioning and rotation ceremonies become required operational products,
  not implementation details.
- A disconnected node cannot learn a revocation or trust update it has not
  received; ADR 0004 defines the conservative behavior for that condition.

## Required records and tests

- Key-generation, custody, activation, rotation, revocation, and destruction
  ceremonies produce immutable audit records without private-key material.
- Golden fixtures cover wrong role, wrong scope, expired key, revoked key,
  unknown key, altered chain, older trust bundle, equal generation/different
  digest, unsupported algorithm, and missing critical fields.
- Loss of any online role key has a documented rotation and fleet-distribution
  drill before production use.

## Unresolved decisions

- The production HSM/KMS vendor and whether a two-provider root backup is required.
- Whether each humanitarian organization receives its own subordinate trust
  domain or shares one root with tightly scoped role keys.
- TPM availability and acceptable software-keystore fallback by hardware profile.
- Maximum role-key lifetimes and rotation overlap windows.
