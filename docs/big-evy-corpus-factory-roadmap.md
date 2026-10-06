# Big EVY Corpus Factory → Lil EVY Roadmap

## Mission

Deliver the right locally relevant information to disconnected Lil EVY nodes,
with evidence that every answerable fact came from an authorized source, passed
human and automated review, and belongs to the active event, geography, language,
audience, and time window.

The Corpus Factory is a connected Big EVY capability. Lil EVY never discovers
internet sources or silently refreshes knowledge in the field. It accepts only
verified corpus releases through a stable, versioned release contract.

```text
Authoritative sources
        ↓
Source registry → immutable evidence vault
        ↓
Extraction → canonical documents → human review
        ↓
Deterministic chunks → optional portable embeddings
        ↓
Retrieval, safety, freshness, and hardware evaluations
        ↓
Independent approval → protected signing → immutable OCI release
        ↓
Connected registry | intermittent mirror | encrypted physical media
        ↓
Lil EVY: receive → stage → verify → index → smoke test → activate
```

## Product boundary

Keep the factory in this repository initially, but isolate it from the edge
runtime. The only shared interface is the signed Corpus Release contract.

- **Big EVY owns:** acquisition, evidence, governance, transformation, review,
  evaluation, signing, publication, revocation, and fleet release inventory.
- **Lil EVY owns:** receipt, offline verification, local indexing, atomic
  activation, rollback, answer attribution, and activation receipts.
- **Humans own:** source authority, licensing, critical conflict resolution,
  sensitivity decisions, local-language approval, evaluation thresholds,
  production promotion, and signing authorization.
- **Agents may:** discover, extract, normalize, translate drafts, flag conflicts,
  produce chunks, propose test cases, run evaluations, and prepare releases.

The runtime must not import factory implementation code. This allows the factory
to become a separate repository or managed service later without redesigning
Lil EVY.

## Trust principles

1. A valid signature proves who released bytes; it does not prove the facts are
   correct, current, safe, or appropriate for a particular location.
2. Raw evidence is immutable. Upstream changes create new evidence snapshots and
   never rewrite a prior release.
3. Canonical documents are approved before chunking. Chunks are derived records,
   never independent facts.
4. Conflicting critical facts are never averaged or silently collapsed.
5. Time-critical facts can expire and force a refusal even while the remainder
   of the corpus remains usable.
6. Full immutable releases come before delta updates. Optimize transfer only
   after snapshot correctness and recovery are proven.
7. Do not distribute a Chroma database. Distribute portable documents, chunks,
   and optional embeddings, then build a new local index.
8. Every answer is attributable to an active release digest and source lineage.

## Versioned contracts to freeze first

### SourceRecord

- Stable source ID, publisher, accountable data steward, and authority class
- Original URL, API, file, or physical reference and acquisition method
- Acquisition and last-verified times
- MIME type, byte size, and SHA-256 of the immutable evidence snapshot
- License and redistribution restrictions
- Geography, language, audience, and subject coverage
- Effective dates, expected refresh frequency, and freshness policy
- Sensitivity and distribution classification
- Allowlisted connector and acquisition-policy version

### CanonicalDocument

- Stable document ID and revision
- Event, deployment, geography, audience, and language scope
- Canonical text and structured facts
- Exact SourceRecord references and source-location citations
- Transformation tools and versions
- `valid_from`, `valid_until`, `review_due_at`, and `stale_action`
- Authority rank, conflict state, resolution explanation, and supersession links
- Safety class and permitted delivery channels
- Reviewer identities, decisions, and approval state

### ChunkRecord

- Deterministic chunk ID and parent document revision
- Exact parent/source span and ordered section path
- Chunker name, version, configuration, and code digest
- Language, token count, inherited scope, and content hash
- No generated facts that do not exist in the canonical parent

### ReviewAttestation

- Subject digest, reviewer identity and role, decision, checklist version, time,
  comments, and independent approvals required for critical material
- Local-language or community validation where applicable

### ReleaseManifest

- Event ID, semantic version, release UUID, and monotonically increasing sequence
- Previous release digest and optional pre-authorized recovery digest
- Created, effective, expiration, and revocation information
- Target site, geography, language, audience, and sensitivity policy
- Minimum Lil EVY runtime and schema/policy/toolchain versions
- Source, document, chunk, and embedding counts
- Chunker and embedding model digest, dimension, and normalization
- Every layer's media type, SHA-256, and byte size
- Evaluation report and approval-attestation digests
- Signing key ID and offline verification bundle

### ActivationReceipt

- Node, cluster, and site identity
- Desired and activated digest/sequence plus previous digest
- Verification policy, runtime, chunker, model, and embedding identities
- State transition, result, reason code, and local smoke-test summary
- Monotonic device counter and time-confidence state
- Device signature; no user messages or personal data

## Authority, conflicts, and freshness

Agents can rank or flag sources but cannot authorize them. Each deployment needs
a policy-owned authority registry. Resolution is scoped by domain, geography,
language, audience, and time; “newer” wins only when its authority permits it.

- An unresolved critical conflict blocks release.
- A noncritical unresolved conflict is marked contested and produces a refusal or
  explicit caveat at runtime.
- `review_due_at` requests revalidation but may permit continued use.
- `valid_until` marks ordinary staleness.
- `stale_action=block` is mandatory for active hazards, shelters, evacuation
  routes, medical instructions, operational contacts, and similar critical facts.
- If trusted time is unavailable, Lil EVY reports degraded freshness and follows
  conservative policy for time-critical content.

Minimum separation of duties:

1. Source owner confirms authority and redistribution rights.
2. Domain or local SME confirms operational facts.
3. Data-protection reviewer clears personal and sensitive non-personal data.
4. Release approver promotes the exact tested digest.
5. Isolated signer signs only a policy-passing digest.

## Release and transport model

One immutable OCI Corpus Release protocol is used for every transport. Registry
tags are discovery aliases; Lil EVY activates only an OCI digest.

### Connected before deployment

Big EVY promotes the digest to Quay. Git contains only a small desired-state
record: digest, event, sequence, policy hash, and rollout ring. RHACM Placement
targets sites by event, geography, language, and risk labels. Nodes pull and
verify the artifact directly; RHACM/GitOps does not carry corpus bytes.

### Intermittently connected

Use pull-based reconciliation and resumable, content-addressed OCI downloads.
The active release remains untouched until the candidate is complete, verified,
indexed, and tested. Signed receipts queue locally until connectivity returns.
A regional Quay mirror can reduce bandwidth and recovery time.

### Fully disconnected

Export an OCI-layout transfer set containing exact corpus, application, model,
signature-bundle, and activation-request digests. Transfer it on encrypted,
serialized, tamper-evident media under chain of custody. The ingestion station
mounts media `noexec,nodev,nosuid`, scans it, imports only signed-manifest blobs
into a site-local registry, and never activates on insertion. The same Lil EVY
verification state machine runs afterward. Receipts can return on outbound media.

SMS and LoRa are not corpus transports. They may carry a small signed urgent
bulletin or an update notification under a separate constrained protocol.

### Confidential releases

When confidentiality is required, encrypt payload layers with AES-256-GCM and
wrap the data key to a site/deployment public key, preferably TPM-bound. Sign the
ciphertext artifact digest so integrity is checked before decryption. Signing
and encryption keys have separate roles and lifecycles.

## Lil EVY activation and recovery

```text
DISCOVERED → DOWNLOADING → STAGED → VERIFIED → INDEXED
           → CANARY_TESTED → READY → ACTIVE
Any stage may become REJECTED or RECOVERY without changing the active release.
```

- Stage into a digest-specific immutable path.
- Verify signatures, key role, hashes, event/site scope, schema/runtime/model
  compatibility, sequence, freshness, classification, and available capacity.
- Build a new index and run local retrieval, no-answer, safety, disk, and latency
  smoke tests.
- Atomically switch a small current-release pointer and wait for readiness.
- Commit the highest activated sequence only after service acknowledgement.
- Retain the active, pre-authorized previous, and small golden emergency corpus.
- A power failure must yield either the old or new state, never a partial index.

Sequence, not wall-clock time, is the primary anti-rollback control. A validly
signed lower or equal sequence is rejected. Ordinary rollback may run a
pre-authorized recovery digest while keeping the higher sequence floor. Any
other downgrade requires a separate site-scoped rollback authorization.

Default failure behavior is to keep serving the last known-good release with a
visible stale/recovery state. Never silently fall back to ungrounded LLM answers.

## Promotion gates

A production release requires:

- Complete schema, provenance, hash, license, scope, and freshness validation
- Human verification of all critical operational facts
- Personal and sensitive-location disclosure review
- Zero unresolved critical conflicts
- Byte-identical deterministic rebuilds of canonical and chunk outputs
- Retrieval Recall@k, MRR, no-answer precision, citation correctness, grounded
  answer quality, contradiction handling, and stale-information refusal
- Results broken down by topic, geography, language, and vulnerable audience
- Storage, indexing, and p50/p95 latency on every supported Lil EVY profile
- Corrupt, unsigned, wrong-event, wrong-site, expired, revoked, and rollback
  attack rejection tests
- Local-language/community acceptance where applicable
- Canary activation and a valid signed receipt before fleet promotion

The artifact evaluated must be byte-for-byte the artifact promoted.

## Agentic delivery model

Use one lead/integration agent and three implementation agents per wave. The lead
owns contracts, compatibility decisions, fixtures, integration, and merge order.
Each implementation agent receives a narrow directory boundary and acceptance
tests. No agent both produces data and approves its trustworthiness.

### Work that can run in parallel after contract freeze

- Source adapters and evidence-vault implementation
- Structured, PDF/text, and multilingual normalizers
- Deterministic chunker and portable embedding writer
- Evaluation harness and adversarial fixtures
- OCI publisher and disconnected export/import tools
- Lil EVY staging, verification, status, and receipt endpoints
- Chaos and power-failure testing

### Work that remains serialized or human-approved

- Contract semantics and compatibility changes
- Authority and conflict-precedence policy
- Licensing and sensitivity classification
- Critical fact and translation approval
- Life-safety ground truth and evaluation threshold changes
- Release promotion and signing-key use
- Emergency revocation and downgrade authorization

## Phased roadmap

### Phase 0 — Contract and trust freeze

Deliver architecture decisions, threat model, key ceremony, v2 JSON Schemas,
release lifecycle, compatibility policy, and golden valid/invalid fixtures.

Acceptance: factory and runtime validate identical fixtures; unknown critical
fields fail closed; schema meaning cannot silently change; signing keys never
enter the repository or general build environment.

### Phase 1 — Evidence intake

Build the declarative source registry, adapter SDK, immutable evidence vault,
network safeguards, audit log, idempotent acquisition, and source-change report.

Acceptance: identical bytes have identical evidence IDs; prior evidence cannot
change; licensing or ownership gaps block promotion; failed acquisition cannot
partially update an event.

### Phase 2 — Canonicalization and review

Build structured and unstructured extractors, exact evidence references,
duplicate/contradiction detection, review queue, precedence rules, translations,
and geographic scoping.

Acceptance: unsupported facts cannot be approved; agent extraction is always a
draft; conflicts remain visible; expired facts cannot enter an active release;
review history identifies who approved what and when.

### Phase 3 — Deterministic retrieval preparation

Build section-aware deterministic chunks, stable IDs/spans, lexical fields,
optional portable embeddings, and full rebuild support.

Acceptance: identical inputs/tool versions are byte-identical; every chunk maps
to evidence; chunker/model changes create new derived identities; canonical
documents remain in the release; no Chroma database is distributed.

### Phase 4 — Evaluation gates

Add event-owned relevant-document ground truth, Recall@k, MRR, no-answer and
citation measures, contradictions, expiration, ambiguity, geography, language,
latency, and resource-profile gates. Bind the report digest into the release.

Acceptance: no-answer and expired-data tests are hard gates; critical topics use
stricter thresholds; regressions compare with the last promoted digest; failures
show the exact expected and retrieved evidence lineage.

### Phase 5 — Release and distribution

Add protected approval/signing, OCI digest publication, encryption envelopes,
offline verification bundles, media import/export, release inventory, key
rotation, and revocation.

Acceptance: signing occurs only after approval and evaluation; online and
offline delivery produce the same digest; modified, incomplete, revoked,
expired, wrong-scope, or lower-sequence artifacts are rejected.

### Phase 6 — Lil EVY activation

Build staging, full policy verification, local smoke tests, atomic activation,
anti-rollback state, known-good recovery, answer attribution, and signed receipts.

Acceptance: failed installation never changes the active corpus; power-loss
recovery is safe; unauthorized downgrade is blocked; health exposes the active
digest/sequence; reconnection reports active, rejected, expired, and recovered
releases.

## First three parallel waves

### Wave 1 — Freeze the contract

- Agent A: v2 schemas, lifecycle, and golden fixtures
- Agent B: threat model, roles, trust roots, signing/encryption/key lifecycle
- Agent C: evaluation and event ground-truth specification
- Lead: reconcile decisions, publish ADRs, and freeze v2

### Wave 2 — Build the factory core

- Agent A: source registry and acquisition adapter SDK
- Agent B: evidence vault and provenance graph
- Agent C: canonicalization, conflict detection, and review records
- Lead: integrate and rehearse one synthetic event

### Wave 3 — Complete the release path

- Agent A: deterministic chunks and embedding lineage
- Agent B: retrieval, safety, freshness, and hardware gates
- Agent C: signed OCI plus connected/intermittent/air-gap delivery
- Lead: integrate Lil EVY activation and run the complete field drill

The first milestone is not scraping a live event. It is a small synthetic
disaster-response release containing conflicting sources, corrections,
expiration, sensitive locations, a missing answer, signature failure, interrupted
transfer, successful activation, rejection, and rollback. A real event is
onboarded only after this drill passes end to end.

## Current foundation and immediate gaps

Already implemented:

- Immutable event/version package directories
- Per-file SHA-256 verification and optional Ed25519 signatures
- Event/version identity checks and read-only runtime APIs
- OCI carrier image and OpenShift init installation
- Corpus-content plus embedding-specific Chroma collections
- Retrieval and end-to-end answer evaluation entry points
- Strict v2 source, canonical-document, chunk, review, release, and receipt
  schemas with golden valid/invalid fixtures
- Strict event/deployment policy packs and deterministic corpus-suitability
  reports that block operationally incomplete corpora even when packaging is valid
- Versioned source registries and bounded HTTPS acquisition with exact host,
  public-network, redirect, media-type, size, timeout, and immutable-output checks
- Deterministic refresh plans plus a canonical, locked, fsync'd hash-chain audit
  ledger for acquisition decisions and report identities
- Allowlisted local intake, immutable content-addressed evidence, registry-owned
  stable identities, canonical provenance, and deterministic exact-span chunks
- Five-layer suitability, release, retrieval, grounded-answer, and edge-profile promotion gate
  bound to exact artifact and runtime digests
- Fail-closed Lil EVY staging, explicit verification results, local indexing,
  smoke testing, atomic activation, monotonic receipt counters, anti-rollback
  sequence floors, authorized recovery, and schema-valid signed receipts
- Trust, signing/encryption, anti-rollback, trusted-time, and separation-of-duty
  architecture decisions
- Scope- and time-aware structured-fact conflict detection with explicit
  supersession and independent critical-review enforcement
- Portable deterministic embedding JSONL with complete model/chunk lineage and
  declared-normalization verification; no vector database files are distributed
- Signed-by-default disconnected transfer sets with closed-world manifests,
  content-addressed artifacts, site/event/classification checks, media replay
  protection, sequence floors, and crash-recoverable atomic import
- Standalone bounded activation-operator HTTP boundary with constant-time
  authentication, intake-root confinement, explicit restart reconciliation,
  and node-held Ed25519 receipt signing
- Signed exceptional-recovery authorization with event/site/state/time/trust
  binding, independent approvers, and durable authorization/nonce replay guards
- Per-answer internal attribution binding retrieval evidence to the active corpus
  digest and sequence without adding a second edge-network request
- Recovery serving-policy enforcement that suppresses blocked and unclassified
  evidence, plus fleet inventory grouped by active digest and sequence
- Opt-in OpenShift activation sidecar with projected-Secret-safe key loading,
  an internal-only Service, and bounded post-restart exact-release reconciliation

Still required before this is production-grade:

- Authenticated API/SFTP adapters, always-on acquisition controller, protected
  external audit anchoring, and OpenShift egress-policy deployment for the HTTPS worker
- Human review UI/identity workflow, translation approval, and deployment-owned
  conflict/freshness policy configuration
- Protected signing service, key rotation, and revocation
- Evaluation attestations bound into the signed artifact
- Encrypted transfer envelopes, production key integration, and media custody
- A deployment controller that executes the OpenShift rollout restart and feeds
  live status observations into the completed reconciliation state machine,
  plus desired-release fleet compliance policy and authenticated heartbeats
- Complete synthetic disaster drill followed by hardware and solar-power tests

## Decisions to make during Wave 1

1. Quay/cosign versus a dedicated OCI artifact service for production releases
2. KMS/HSM and offline-root ownership model
3. Evidence-vault storage and retention policy
4. Review UI/workflow and identity provider
5. Data classification levels and permitted answer channels
6. Supported languages and trusted translation reviewers
7. Minimum retrieval/no-answer thresholds by safety class
8. Trusted-time strategy for disconnected nodes
9. Site identity and TPM availability for encryption and receipts
10. Retention limits for old corpora, indexes, evidence, and audit records
