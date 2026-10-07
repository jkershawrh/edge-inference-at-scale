# Connected acquisition controller

The connected Big EVY environment can reconcile approved HTTPS sources
without turning the Corpus Factory into a crawler. The controller reads one
validated source registry and its matching event policy, considers only source
IDs declared there, and enforces a maximum of 64 sources per cycle. It does not
discover links, widen host rules, follow ambient proxies, or fetch unregistered
URLs.

## Safety boundary

Before any network request, the existing acquisition contract verifies source
approval, authority, publisher, geography, audience, language, licensing,
validity, HTTPS-only transport, exact allowed hosts, and globally routable DNS.
Every redirect is rechecked. Payload bytes, media types, redirect counts,
timeouts, and response sizes remain bounded by the registry.

Connectors naming `auth_secret_ref` fail closed before transport. Authenticated
API and SFTP adapters remain future, separately reviewed implementations; this
controller does not interpret secret references or silently fall back to
anonymous access.

Each attempted source writes a tamper-evident `acquisition_planned` event before
network access. Success and failure both produce a content-addressed durable
outcome and a chained audit event. Successful snapshots update an atomically
written state file, allowing the next pass to distinguish initial, changed, and
unchanged content. A crash can repeat an idempotent fetch, but cannot overwrite
content-addressed evidence or skip audit-chain verification.

## Local operation

A one-source operation is explicit:

```bash
python3 scripts/run_acquisition_controller.py \
  --registry prepared/source-registry.json \
  --event-policy prepared/event-policy.json \
  --state-root var/corpus-factory \
  --actor urn:evy:operator:alice \
  --mode one-shot \
  --source-id source-shelter-primary
```

A bounded reconcile lifecycle processes enabled registry entries in stable
source-ID order. This example performs twelve passes and then exits:

```bash
python3 scripts/run_acquisition_controller.py \
  --registry prepared/source-registry.json \
  --event-policy prepared/event-policy.json \
  --state-root var/corpus-factory \
  --actor urn:evy:controller:connected \
  --mode reconcile \
  --max-sources 32 \
  --max-cycles 12 \
  --interval-seconds 300
```

Any failed source makes the cycle and process fail. Other selected sources still
receive durable outcomes, so operators can repair a single registry or upstream
problem without losing evidence from the rest of the pass.

## OpenShift connected profile

Build `corpus_factory/Containerfile.acquisition`, create reviewed ConfigMaps
named `corpus-source-registry` and `corpus-event-policy`, then apply:

```bash
oc apply -k deploy/corpus-factory-acquisition/profiles/openshift-connected
```

Pin the controller image to the reviewed registry digest before applying a
production overlay; the checked-in `latest` value is only a build placeholder.

The profile runs one non-root writer against a persistent volume. Each process
lifecycle is bounded to 288 five-minute cycles; the Deployment restarts it after
that lifecycle. It has no Kubernetes API token, no Service, and no Route.

Network control has two layers:

- NetworkPolicy denies ingress and all unspecified egress, allowing only
  OpenShift DNS and TCP 443.
- The namespace `EgressFirewall` permits TCP 443 only to reviewed DNS names,
  then denies all IPv4 and IPv6 destinations.

The checked-in `alerts.example.gov` rule is an example aligned with the test
registry. Before deployment, replace the allow rules through reviewed Git
changes so they exactly match enabled `allowed_hosts`. The EgressFirewall and
registry are intentionally separate controls: a destination must pass both.
Do not deploy this profile into a namespace that already owns an
`EgressFirewall`; OpenShift permits only one per namespace.
