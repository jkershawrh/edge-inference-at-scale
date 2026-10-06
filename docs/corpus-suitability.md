# Event-specific corpus suitability

## Design boundary

The Corpus Factory is reusable across conferences, floods, wildfires, public
health responses, rural services, and other deployments. Its schemas, lineage,
chunking, signing, and evaluation machinery do not encode one domain.

The decision to deploy a corpus is deliberately event-specific. A generic
quality score cannot establish that the right shelter, water, medical, or
evacuation facts are present for a particular place and time. Every deployment
therefore supplies a versioned `event_policy` record. Promotion fails unless the
candidate satisfies that policy exactly.

## What the event policy declares

An event policy identifies:

- deployment IDs, geographies, languages, audiences, and delivery channels;
- registered authorities, publishers, source IDs, subjects, and geographic
  authority boundaries;
- required facts or canonical documents and their safety classifications;
- minimum approved documents and independent sources;
- freshness limits and the action to take when information is stale;
- positive, boundary, and no-answer evaluation-case minimums;
- safe refusal and escalation behavior; and
- independent human approvals for critical policy versions.

Critical policies fail closed. They require critical coverage, blocking stale
behavior, and independent domain/local review. The policy body has its own
digest so approvals can sign the decision content without a self-referential
record hash.

## Deterministic suitability gate

`corpus_factory.suitability.evaluate_suitability` validates the policy and each
source and canonical document, then evaluates every declared intersection of
geography, language, audience, and channel. Evidence qualifies only when it is:

- authorized by the exact registered source, publisher, and authority class;
- traceable through matching immutable evidence digests;
- approved, uncontested, in-scope, and valid at the trusted evaluation time;
- recent enough for the requirement's maximum source age; and
- supported by the required number of independent sources and approved cases.

The report is deterministic and records policy, source-inventory,
document-inventory, and evaluation-case digests. Any unmet condition produces a
machine-readable failure and a `FAIL` decision. A passing report becomes a
required input to the five-layer promotion gate; a correctly signed package
without adequate operational content cannot be promoted.

## Connected Big EVY workflow

Use the connected environment to acquire and review source evidence. Then run:

```bash
python3 scripts/evaluate_corpus_suitability.py \
  --policy prepared/event-policy.json \
  --sources prepared/sources.json \
  --documents prepared/canonical-documents.json \
  --evaluation-cases prepared/evaluation-cases.json \
  --as-of 2026-10-06T18:00:00Z \
  --time-confidence trusted \
  --output evidence/suitability-report.json
```

Exit status `0` means the policy was satisfied, `1` means the candidate was
validly evaluated but unsuitable, and `2` means the inputs could not be
evaluated. The report is evidence, not the corpus payload itself. The release
promotion command must receive that report along with the other four gate
reports.

The synthetic disaster-response drill demonstrates the intended development
loop: an incomplete candidate fails, the missing approved material and cases
are added, and the corrected candidate passes under the same policy. Run it
with:

```bash
make test-corpus-suitability
```

## Human responsibility

Automation can verify declared coverage, lineage, freshness, and test counts.
It cannot decide which authority is legitimate, what facts are operationally
necessary, whether a translation is safe, or whether a local escalation route
is appropriate. Those choices remain explicit, reviewable human inputs to the
event policy and source registry. Production signing keys and release approval
also remain outside autonomous agents.
