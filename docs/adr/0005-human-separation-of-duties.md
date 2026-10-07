# ADR 0005: Human authority and separation of duties

- Status: Proposed for Wave 1 contract freeze
- Date: 2026-10-06
- Depends on: ADRs 0001 and 0002
- Scope: Big EVY source approval, release promotion, and emergency operations

## Context

Automation and agents can discover, extract, normalize, translate, compare, and
test large amounts of information. They cannot determine whether a publisher is
legally authoritative, whether a sensitive location should be disclosed, or
whether a translated instruction is safe for a local community. A technically
valid artifact can still be factually wrong or operationally dangerous.

The pipeline needs human accountability without allowing one person, service
account, or agent to source, approve, sign, and promote its own output.

## Decision

### Human roles are explicit attestations

Production policy recognizes these roles:

| Role | Required decision |
| --- | --- |
| Source owner/data steward | Source authority, ownership, license, redistribution rights, expected freshness |
| Domain or local SME | Operational correctness, geography, audience, conflicts, life-safety facts |
| Data-protection reviewer | Personal data, vulnerable-person data, sensitive locations, permitted channels |
| Language/community reviewer | Meaning, terminology, literacy, cultural and local operational suitability |
| Evaluation owner | Ground truth, thresholds, exceptions, and interpretation of retrieval/safety results |
| Release approver | Promotion of the exact tested digest under a named policy |
| Signing operator/service | Cryptographic signing of only the approved digest |
| Recovery approver | Exceptional downgrade, state repair, or emergency restriction |

Each decision is a signed ReviewAttestation binding the subject digest, event and
scope, reviewer identity and role, decision, checklist/policy version, timestamp,
comments or reason code, and any expiry or conditions. Editing the subject
invalidates the attestation.

### Critical facts require independent approval

Life-safety, high-sensitivity, and operationally critical content requires at least:

1. approval by the accountable source owner/data steward;
2. approval by an independent domain or local SME; and
3. data-protection approval when personal data, sensitive locations, or channel
   restrictions are present.

A language/community reviewer is additionally required for every translated
critical instruction. Machine translation is always a draft. The source owner
and SME may not be the same identity for critical content. The release approver
may not supply either required critical-content approval.

Noncritical public reference content may use one source approval plus automated
evaluation when the signed event policy explicitly allows it.

### Production promotion uses four-eyes control

The release approver selects an exact candidate digest that has passed all gates.
The signing service independently verifies the policy, attestations, evaluation
digest, and candidate digest before signing. The signing operator/service cannot
modify candidate content or waive a failed gate.

No identity may both:

- author or transform a subject and provide its only required review;
- own the evaluation ground truth and unilaterally waive its failed threshold;
- approve a release and authorize its exceptional downgrade;
- administer the signing policy and alone invoke a production signature; or
- issue a recovery authorization and be its sole required approver.

Production signing and exceptional recovery each require two distinct human
identities authenticated through the organization's identity provider. Service
accounts act only as constrained executors and are attributed to the approving humans.

### Agents prepare evidence but never confer trust

Agents may discover sources, capture immutable evidence, extract and normalize
content, propose authority rankings, draft translations, flag conflicts, create
chunks, suggest evaluations, and assemble unsigned candidates.

Agents may not:

- mark a source authoritative or approve redistribution rights;
- resolve a critical factual conflict;
- approve critical facts, translations, sensitivity, or disclosure channels;
- change production evaluation thresholds or waive a failed gate;
- approve, sign, revoke, or promote a production release; or
- authorize downgrade, state recovery, or key use.

Agent identity, model/tool version, prompt/workflow version, and output digest are
recorded as transformation provenance, not as a human ReviewAttestation.

### Conflicts and exceptions stay visible

An unresolved critical conflict blocks promotion. Noncritical contested content
must carry its conflict state and runtime refusal/caveat policy. Reviewers cannot
erase competing evidence; they record precedence, rationale, scope, and duration.

An emergency exception is a new signed attestation, never an audit-log edit. It
names the failed or bypassed control, incident, exact digest, restricted scope,
expiry, compensating controls, and two independent approvers. Exceptions cannot
waive invalid signatures, digest failures, wrong site/event scope, unresolved
critical conflicts, or anti-rollback requirements. Life-safety factual approval
still requires an SME.

### Access and audit are least-privilege

- Source intake, review, release approval, signing administration, and recovery
  authorization are separate permission groups.
- Temporary elevation is time-bound and recorded.
- Attestation and signing audit logs are append-only, retained independently from
  the release workspace, and exportable for disconnected inspection.
- Removing an operator disables future actions but does not invalidate historical
  attestations; explicit revocation names affected attestations or releases.
- Review UI and APIs display source evidence and exact diffs, not only generated
  summaries, before approval.

## Consequences

- Production throughput is intentionally bounded by qualified reviewer capacity.
- Critical releases cannot be fully autonomous, even if extraction and evaluation are.
- Smaller deployments may need cross-organization reviewers to achieve independence.
- Every promoted fact and release has a named accountable decision trail.
- Emergency operation remains possible, but exceptions are narrow, expiring,
  visible, and cannot bypass cryptographic integrity.

## Required records and tests

- Policy tests reject missing roles, duplicate identities in incompatible roles,
  stale attestations, wrong subject digests, unapproved translations, unresolved
  critical conflicts, failed gates without a permitted exception, and a signer
  acting without exact-digest approval.
- The first synthetic disaster corpus includes conflicting official sources,
  expired shelter data, a sensitive location, a translated instruction, and a
  failed retrieval gate so each human boundary is exercised.
- Quarterly access review and an annual key/recovery exercise are required before
  a production trust profile can remain active.

## Unresolved decisions

- The identity provider, attestation format, and review workflow product.
- Which safety classifications require two SMEs rather than one SME plus a source owner.
- How small local organizations establish independent review without delaying
  urgent releases.
- Retention periods and privacy rules for reviewer identities and comments.
- The emergency approval quorum and maximum exception duration by deployment type.
