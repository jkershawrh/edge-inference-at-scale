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
