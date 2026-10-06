# Controlled model and RAG experiments on OpenShift

The experiment harness compares model/runtime candidates without changing the
corpus, embedding model, evaluation set, retrieval depth, or resource profile.
It refuses to compare reports when those controls differ.

## Experiment rule

Change one primary variable at a time: the model plus the runtime required to
serve it. Keep the exact signed corpus digest and embedding identity constant.
Run separate comparison groups for each OpenShift resource profile.

The harness records:

- End-to-end response-quality pass rate
- Pipeline p50/p95/max latency
- Retrieval evidence Recall@k and mean reciprocal rank
- Retrieval p50/p95/max latency
- Request failures
- API, LLM, and RAG health/statistics snapshots
- Expected and observed model identity

Physical watts, thermal throttling, radio behavior, and accelerator performance
cannot be inferred from OpenShift CPU quotas. Those require later hardware runs.

## Run one scenario

Deploy the exact corpus digest and one OpenAI-compatible model runtime using the
selected Helm resource profile. Configure the scenario in
`tests/benchmarks/experiment_matrix.yaml`, then expose or port-forward the API:

```bash
export EDGE_API_URL=https://edge-api.apps.example.test
export CORPUS_DIGEST=sha256:REPLACE_WITH_THE_ACTIVATED_OCI_DIGEST

python3 tests/benchmarks/run_edge_experiment.py \
  --scenario bitnet-small-control \
  --output dist/experiments/bitnet-small.json
```

Repeat after deploying the candidate to the same comparison group:

```bash
python3 tests/benchmarks/run_edge_experiment.py \
  --scenario candidate-small \
  --output dist/experiments/candidate-small.json
```

The runner checks the model reported by `/llm/health` against the declared model.
Candidate placeholders must be replaced with the exact artifact/quantization and
runtime before execution.

## Compare candidates

```bash
python3 tests/benchmarks/compare_edge_experiments.py \
  dist/experiments/bitnet-small.json \
  dist/experiments/candidate-small.json \
  --output dist/experiments/small-comparison.json
```

Comparison fails if the resource profile, corpus digest, embedding model,
evaluation-set digest, retrieval depth, or comparison group differs. This keeps
a fluent answer from concealing a corpus change or a larger resource allocation.

Run at least three repetitions after warm-up before making a model decision.
Retain every raw report with the model artifact digest, runtime image digest,
OpenShift node identity, and eventual hardware/power measurements.

## Run the OpenShift convergence gate

The live gate is deliberately stricter than the standalone comparison runner.
It first performs a read-only preflight against the selected Helm release. The
preflight verifies all core Deployments are ready, the declared Route matches,
field safety is enabled, the active signed corpus digest matches, and the live
embedding and LLM identities match the experiment declaration. It also retains
resolved container image IDs without reading Secrets or message content.

```bash
export EDGE_NAMESPACE=lil-evy-lab
export EDGE_RELEASE=lil-evy
export EDGE_API_URL=https://lil-evy-api-gateway-lil-evy-lab.apps.example.test
export EDGE_RESOURCE_PROFILE=lab-small
export CORPUS_DIGEST=sha256:REPLACE_WITH_THE_ACTIVE_DIGEST
export CORPUS_MODE=packaged # use activation for an activated Lil EVY release
export CORPUS_EVENT_ID=summit-connect
export CORPUS_VERSION=2026.1
export EMBEDDING_MODEL=all-MiniLM-L6-v2
export LLM_PROVIDER=bitnet
export LLM_MODEL=bitnet-2b4t
export CHANNEL_DRIVER=simulator

make test-openshift
```

The gateway Route is used for retrieval, answer-quality, and capacity checks.
Generated evidence is written beneath `artifacts/` and should be retained by CI
or copied to the release record. A missing workload, identity mismatch,
unhealthy service, unavailable model, inactive corpus, or disabled field-safety
setting is RED. OpenShift results remain AMBER for physical GSM, radio, power,
thermal, and human field qualification.

`CORPUS_MODE=packaged` accepts only a signature-enforced package whose resolved
init-container image digest, event, and version match the declaration.
`CORPUS_MODE=activation` additionally requires the live anti-rollback activation
pointer to report the exact digest as ready. An unpackaged corpus can never pass
the field EDD preflight.
