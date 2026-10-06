# Connected Big EVY source acquisition

## Purpose

The connected Corpus Factory acquires evidence only from a versioned source
registry. A URL discovered by an agent is a proposal; it cannot be fetched by
the production pipeline until a human-controlled registry version identifies
the publisher, steward, authority, rights, scope, freshness policy, connector,
and network limits.

The first connector is deliberately narrow: unauthenticated HTTPS documents.
API authentication, SFTP, browser automation, and live scraping remain disabled
until each has its own secret, network, pagination, and evidence-boundary policy.

## Registry contract

`source-registry.schema.json` requires an immutable registry identity and
semantic version, event scope, acquisition-policy version, and unique source and
connector IDs. Each enabled source declares:

- an exact HTTPS URL and exact allowed host list;
- allowed response media types;
- response byte, timeout, and redirect limits;
- a secret reference rather than embedded credentials;
- publisher, steward, authority class, license, and redistribution rights;
- geography, language, audience, and subject scope; and
- effective, expiration, refresh, and stale-action policy.

The registry digest is written into every acquisition report. Changing a URL,
authority, scope, limit, or license therefore changes the evidence surrounding
the acquisition even when the downloaded bytes are identical.

Before network access, every enabled registry source is also reconciled with the
approved event policy. Event identity, source ID, publisher, authority class,
authorized subjects and geographies, and deployment geography/language/audience
scope must agree. This prevents a syntactically valid connector registry from
quietly expanding who or what the event trusts.

## Acquisition behavior

Before every request and redirect, the adapter requires HTTPS on port 443,
rejects credentials, fragments, literal IP hosts, unlisted hosts, and DNS
answers that are private, loopback, link-local, reserved, or otherwise not
globally routable. Automatic redirects and ambient HTTP proxies are disabled.
Responses must be successful, nonempty, within the declared byte limit, match
their Content-Length when supplied, and use an allowlisted media type.

Only after all checks pass are bytes written to the shared content-addressed
evidence vault. Identical bytes reuse the same immutable snapshot; changed bytes
create a new snapshot without replacing the old one. A deterministic report
classifies the result as `initial`, `unchanged`, or `changed` and binds it to the
registry, connector, final URL, observation time, and evidence digest.

Example:

```bash
python3 scripts/acquire_corpus_source.py \
  --registry prepared/source-registry.json \
  --event-policy prepared/event-policy.json \
  --source-id source-shelter-primary \
  --evidence-store vault/evidence \
  --observed-at 2026-10-06T18:00:00Z \
  --output-dir runs/2026-10-06T180000Z
```

For later refreshes, pass the previous validated SourceRecord with
`--previous-source-record`. Output records are created immutably; the command
will not overwrite an existing run.

## Refresh planning

Refresh timing is calculated before network access and saved as a versioned,
digest-bound `refresh_plan`. The plan validates the registry against the event
policy, validates every previous SourceRecord, and classifies each source as
`due`, `not_due`, or `disabled`. A source becomes due when it has never been
acquired, its declared refresh interval has elapsed, or its connector URL,
connector identity, or acquisition-policy version changed.

```bash
python3 scripts/plan_corpus_refresh.py \
  --registry prepared/source-registry.json \
  --event-policy prepared/event-policy.json \
  --previous-source-record active/source-shelter-primary.source-record.json \
  --as-of 2026-10-06T18:00:00Z \
  --output plans/refresh-2026-10-06T180000Z.json
```

The planner does not fetch, overwrite, or activate anything. An external
scheduler may execute only the `due_source_ids` from an immutable plan.

## Tamper-evident audit ledger

The audit ledger is canonical JSONL with contiguous sequences and a SHA-256
chain from each event to the previous entry. Appends take an exclusive file
lock, verify the complete existing chain, append one event, flush it, and call
`fsync`. Events contain identities and artifact/report digests, never downloaded
content or secrets.

```bash
python3 scripts/corpus_audit.py append state/acquisition-audit.jsonl \
  --event-type acquisition_planned \
  --occurred-at 2026-10-06T18:00:00Z \
  --actor urn:example:factory-scheduler \
  --subject-id registry-field-event-2026 \
  --payload-digest sha256:REPLACE_WITH_PLAN_DIGEST

python3 scripts/corpus_audit.py verify state/acquisition-audit.jsonl
```

The chain detects modification, deletion within an anchored chain, reordering,
noncanonical records, duplicate keys, gaps, and backwards timestamps. To detect
truncation of the newest entries, periodically anchor the returned last-entry
digest in protected external storage. A local hash chain without such an anchor
cannot prove that an attacker did not rewrite the entire file.

## Production boundary

Application checks are one layer, not the entire SSRF defense. Run acquisition
workers in a dedicated OpenShift namespace with default-deny egress, approved
DNS resolvers, destination allowlists, response-size limits at the proxy, no
service-account access to cluster APIs, and no route to cloud metadata or
internal services. The current preflight DNS check cannot alone eliminate a DNS
rebinding race inside a general-purpose HTTP library.

The pipeline now emits versioned acquisition reports, deterministic refresh
plans, and a tamper-evident local audit chain. It does not yet provide an
always-on scheduler/controller, protected external audit anchoring,
authenticated API connectors, or review-queue integration. Those are the next
connected Big EVY increments.
