# Event corpus packaging and rollout

Each event corpus is an immutable release artifact. Its identity is the pair
`eventId/version`; that pair must never be rebuilt with different content. Create
a new version for every correction, schedule change, or emergency-data update.

## Build and sign

Generate an Ed25519 key once and store the private key outside the repository:

```bash
openssl genpkey -algorithm ED25519 -out corpus-signing-key.pem
openssl pkey -in corpus-signing-key.pem -pubout -out corpus-public-key.pem
```

Build the package with an explicit event and version:

```bash
python3 scripts/package_corpus.py \
  --event-id summit-connect \
  --event-name "Summit Connect 2026" \
  --version 2026.1 \
  --signing-key /secure/path/corpus-signing-key.pem
```

For any non-Summit event, pass a normalized JSON list containing
`{"id": "...", "text": "...", "metadata": {...}}` records:

```bash
python3 scripts/package_corpus.py \
  --event-id flood-response-region-4 \
  --event-name "Region 4 Flood Response" \
  --version 2026.3 \
  --input-documents prepared/flood-response.json \
  --signing-key /secure/path/corpus-signing-key.pem
```

The package contains normalized documents, categories, an index, `manifest.json`,
and `manifest.sig`. The manifest records every file's SHA-256 digest and byte size.
The RAG service refuses packages with a wrong event, wrong version, changed file,
invalid document count, or invalid signature.

Build and push a dedicated carrier image. Use an immutable registry digest for
deployment so a tag cannot be replaced underneath an edge fleet:

```bash
podman build \
  --build-arg CORPUS_PATH=dist/corpora/summit-connect/2026.1 \
  -f corpus/Containerfile \
  -t registry.example/edge-corpus/summit-connect:2026.1 .
podman push registry.example/edge-corpus/summit-connect:2026.1
```

## OpenShift rollout

Store only the public key in the cluster:

```bash
oc create secret generic corpus-signing-key \
  --from-file=public-key.pem=/secure/path/corpus-public-key.pem
```

Configure the exact package. Prefer the digest returned by the registry push:

```yaml
rag:
  corpus:
    enabled: true
    image: registry.example/edge-corpus/summit-connect@sha256:REPLACE_ME
    eventId: summit-connect
    version: "2026.1"
    requireSignature: true
    publicKeySecretName: corpus-signing-key
```

The init container installs the package into
`/data/corpora/<eventId>/<version>`. Existing versions remain available on the
volume, so rollback means deploying the previous image digest and version. It
will not overwrite an existing version with different manifest bytes.

Each event/version also receives a separate Chroma collection keyed by the
corpus content hash and embedding identity. Old and new pods therefore cannot
rewrite each other's vector index during a rolling update, and rollback reuses
the prior index rather than rebuilding it.

For Lil EVY activation, a previous digest can be served only through an explicit
site-scoped recovery authorization. The monotonically increasing sequence floor
is never lowered; merely redeploying an older version does not authorize a
downgrade.

The active packaged corpus is read-only at the API level: add, bulk-add, and
delete operations return HTTP 403. Event changes must go through a new reviewed
package version, which keeps every fleet node reproducible.

## Serve the locally activated release

After a disconnected transfer is verified and the Lil EVY activation manager
has atomically advanced `/data/activation/current.json`, deploy the RAG service
in activation-managed mode:

```yaml
rag:
  corpus:
    eventId: flood-response-region-4
    version: "2026.3"
    publicKeySecretName: corpus-signing-key
  activation:
    enabled: true
    root: /data/activation
```

On every process start, the RAG service resolves the active pointer, enforces
the anti-rollback sequence floor and activation state, rejects path escapes and
symbolic links, checks that the pointer digest is the manifest digest, and then
revalidates the package hashes and Ed25519 signature. Any disagreement prevents
the service from starting; it never silently falls back to an unpackaged corpus.

`GET /activation/status` exposes the bounded active digest, sequence, floor,
mode, and readiness state without exposing filesystem paths or package content.
This runtime changes releases at a deliberate service restart after activation.
Callers cannot activate arbitrary local paths through the RAG API.

The standalone activation-operator application wraps the control core with a
bounded JSON HTTP interface. It uses constant-time bearer authentication,
confines every candidate beneath a configured local intake root, rejects
symlink traversal, and returns only a bounded receipt projection. A successful
request returns HTTP 202 and explicitly reports that restart or reconciliation
is required; it never claims to have live-reloaded the RAG process. Device
receipts can be signed by a node-held Ed25519 key.

The chart can run that operator as an opt-in sidecar beside the RAG process. It
shares the activation volume but has a separate internal-only ClusterIP Service;
the public OpenShift Route never exposes it. The bearer credential and node
receipt private key are mounted from separate Secrets, not placed in a ConfigMap
or environment value. OpenShift projected-Secret links and group-readable
`0440` key files are accepted, while escaping links and writable group/other
permissions fail closed.

```yaml
rag:
  corpus:
    eventId: flood-response-region-4
    version: "2026.3"
    publicKeySecretName: corpus-signing-key
  activation:
    enabled: true
    root: /data/activation
    operator:
      enabled: true
      intakeRoot: /data/activation-intake
      clusterId: relief-cluster-01
      siteId: region-4-site-01
      bearerSecretName: activation-operator-token
      receiptSecretName: activation-receipt-key
      receiptKeyId: region-4-node-001
      policyDigest: sha256:REPLACE_WITH_64_HEX_CHARACTERS
      runtimeVersion: "1.0.0"
      chunkerDigest: sha256:REPLACE_WITH_64_HEX_CHARACTERS
      modelDigest: sha256:REPLACE_WITH_64_HEX_CHARACTERS
      embeddingModelDigest: sha256:REPLACE_WITH_64_HEX_CHARACTERS
```

The token Secret must expose `bearer-token` and the receipt Secret must expose
`private-key.pem` unless their key names are overridden in values. The token is
32–512 printable ASCII characters. The receipt key is an Ed25519 private key.

HTTP 202 means the signed package was durably activated; it does **not** mean
the running RAG process serves it. The deployment controller must
restart the RAG pod and compare `GET /activation/status` with the receipt's exact
digest and sequence. Reconciliation succeeds only after a post-restart
production status reports `ACTIVE`, `ready=true`, and `READY`. Attempts and time
are bounded, and a wrong or recovery-mode target fails rather than being called
successful. The crash-resumable rollout controller persists intent before each
external operation, reuses stable idempotency keys after ambiguous failures,
and enforces durable attempt, poll, and time limits. Its dependency-light
OpenShift adapter patches only the configured RAG Deployment and binds the pod
template to the exact rollout operation, digest, and sequence. The remaining
deployment work is to package the controller with its activation/status HTTP
adapters and narrowly scoped OpenShift RBAC.

Exceptional downgrade uses a separately signed recovery authorization bound to
the event, site, current digest, sequence floor, target digest, trust generation,
validity window, and two independent approvers. Authorization ID and nonce are
single-use. Recovery restrictions are atomically bound to the active pointer.
The RAG service removes both explicitly blocked and unclassified evidence before
it reaches the message router, so Lil EVY does not guess that missing metadata
means safe during exceptional recovery.

## Promotion gates

Before promoting a package to field nodes:

1. Evaluate the candidate inventory against its signed event policy and retain
   the suitability report with the release evidence.
2. Deploy the exact candidate to the OpenShift lab namespace.
3. Run `make test-retrieval-evaluation` and retain the JSON result with the release.
4. Run `make test-evaluation` against the selected LLM.
5. Evaluate the declared edge resource profiles and canary activation.
6. Promote the same corpus image digest—do not rebuild it.
7. Roll out to a canary node/site before the rest of the fleet.

The promotion decision consumes all five reports: corpus suitability, release
validity, retrieval, grounded-answer, and edge operation. Each report is bound
to the same immutable policy, source, document, evaluation-case, release,
model, embedding, and chunker identities. No layer can compensate for another.
See [corpus-suitability.md](corpus-suitability.md) for the event-policy contract
and the connected Big EVY workflow.

Event-specific preparation can evolve independently as long as it emits the
normalized input format. The runtime and Helm rollout do not depend on Summit
Connect-specific filenames.

## Big EVY v2 factory and disconnected transfer

The v1 package above remains the runtime-compatible carrier while the v2 supply
chain is integrated. The v2 factory adds source authority, immutable evidence,
canonical document and chunk lineage, human review, portable embeddings, exact
promotion evidence, sequence enforcement, and signed activation receipts.

Disconnected media carries exactly five content-addressed artifacts:
application, model, corpus, signature bundle, and activation request. Production
verification requires a trusted Ed25519 public key; hash-only verification is a
lab diagnostic and must be requested explicitly.

```bash
python3 scripts/verify_transfer_set.py /media/evy-transfer \
  --event-id flood-response-region-4 \
  --site-id region4-site-alpha \
  --allow-classification restricted \
  --sequence-floor 12 \
  --public-key /etc/evy/trust/transfer-public-key.pem
```

Verification is read-only. Import then performs the same checks, rejects reused
media and non-advancing sequences, and atomically places the set into the site's
content-addressed inbox. SMS and LoRa are never used to transfer these release
bytes.
