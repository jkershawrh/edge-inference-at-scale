# Edge Inference at Scale

**Message → RAG/LLM → Message** | Offline AI at the edge

> In a warzone, disaster zone, or underserved community — anywhere a local radio or 2G link works — people can request knowledge and get a response without internet access.

## What This Is

A reference architecture and live demo showing how to deploy resource-constrained inference and **Retrieval-Augmented Generation** at the edge. BitNet remains the small CPU control model, while the inference boundary can target any local OpenAI-compatible runtime for model and hardware evaluation.

The demo scenario is a conference assistant for **Summit Connect**, but the architecture works for any edge deployment: disaster relief coordination, community information hubs, agricultural advisories, health triage — anywhere information access matters and infrastructure is limited.

## Architecture

```
  ┌───────────────────────────────────────────────────────────────┐
  │  RHEL Image Mode (bootc) + MicroShift                        │
  │  ─────────────────────────────────────                        │
  │                    Edge Node                                  │
  │                                                               │
  │   SMS In ──► SMS Gateway ──► Message Router                   │
  │   (GSM/Twilio)  (Redis         │         │                    │
  │                  Streams)       ▼         ▼                   │
  │   SMS Out ◄──────────── RAG Service   LLM Inference           │
  │                        (OpenVINO +    (OpenAI-compatible      │
  │                         MiniLM)       local provider)         │
  │                                                               │
  │   Privacy Filter     Redis Streams     ChromaDB               │
  ├───────────────────────────────────────────────────────────────┤
  │  Intel Xeon 6 — AVX-512 (BitNet) · OpenVINO (RAG) · AMX/TDX │
  └───────────────────────────────────────────────────────────────┘
```

See [docs/architecture.md](docs/architecture.md) for the full architecture document including the Red Hat + Intel technology mapping, deployment tiers, scaling model, and node profiles.

## Technology Stack

### Red Hat

| Technology | Role |
|-----------|------|
| **RHEL Image Mode (bootc)** | Immutable OS — nodes boot from a pre-built image, zero-touch provisioning |
| **MicroShift** | Lightweight single-node Kubernetes derived from OpenShift (~800MB overhead) |
| **RHACM** | Fleet management — policies, model updates, monitoring across all edge nodes |
| **UBI9** | Security-hardened container base images for all workloads |

### Intel

| Technology | Role |
|-----------|------|
| **AVX-512 / VNNI** | BitNet ternary inference — integer add/subtract, no floating point needed |
| **OpenVINO** | RAG embeddings — MiniLM INT8 quantized, 2-3x faster than PyTorch on Intel CPUs |
| **AMX** | Heavier model inference — 2,048 INT8 ops/cycle per core on Xeon 6 |
| **TDX** | Confidential AI — hardware-isolated VMs with encrypted memory for sensitive data |

### Application

| Layer | Technology | Why |
|-------|-----------|-----|
| **LLM** | OpenAI-compatible local provider | Compare BitNet, GGUF models, and accelerator runtimes without changing the pipeline |
| **RAG** | ChromaDB + MiniLM-L6-v2 (OpenVINO) | Lightweight vector search for domain knowledge |
| **Services** | FastAPI (Python) on UBI9 | Microservice architecture |
| **SMS** | Simulated (Twilio-ready) | GSM modem or Twilio webhook in production |
| **Event Stream** | Redis Streams | Ordered, persistent message delivery with backpressure |

## Quick Start (Development)

```bash
# Clone and start the edge node
git clone https://github.com/YOUR_ORG/edge-inference-at-scale.git
cd edge-inference-at-scale
docker compose up    # requires x86_64 (Intel/AMD) for BitNet

# Send a simulated SMS
./scripts/send_sms.sh "What sessions are about edge computing?"

# Or curl directly
curl -X POST http://localhost:8000/sms/receive \
  -H "Content-Type: application/json" \
  -d '{"sender": "+1234567890", "receiver": "+1000000000", "content": "What sessions are about edge computing?"}'
```

No frontend on the node — it's pure backend, like a real edge deployment. Metrics exposed via API:

```bash
curl http://localhost:8000/services/health     # All services
curl http://localhost:8000/llm/stats           # Inference latency
curl http://localhost:8000/router/statistics   # Throughput + RAG/LLM/delivery stage timing
```

### Provider and resource experiments

BitNet remains the default control. Point the same application image at another
OpenAI-compatible local server with environment variables:

```bash
LLM_PROVIDER=llama-cpp \
LLM_BASE_URL=http://model-server:8080 \
LLM_MODEL=ministral-3b-q4 \
docker compose up
```

The Helm chart includes three OpenShift laboratory envelopes. They validate
memory floors, concurrency, queueing, RAG quality, and latency; CPU quotas do
not predict ARM/NPU speed or physical power draw.

```bash
helm upgrade --install edge-inference chart/ \
  -f chart/profiles/values-lab-small.yaml

helm upgrade --install edge-inference chart/ \
  -f chart/profiles/values-lab-balanced.yaml

helm upgrade --install edge-inference chart/ \
  -f chart/profiles/values-lab-ventuno-class.yaml
```

For disaster, rural, or conflict-zone behavior, also apply
`chart/profiles/values-field-safety.yaml`. It prevents ungrounded LLM fallback
and routes emergency questions through approved local RAG evidence. See
[docs/field-safety-mode.md](docs/field-safety-mode.md).

Use the controlled experiment runner in
[docs/model-rag-experiments.md](docs/model-rag-experiments.md) to compare BitNet
and candidate runtimes without accidentally changing the corpus, embeddings,
evaluation set, retrieval depth, or resource envelope.

For two-way conversational testing before GSM hardware is available, use the
signed Discord `/ask` adapter described in
[docs/discord-testing.md](docs/discord-testing.md). Discord is only a development
transport; the core router now uses a channel-neutral envelope so the field SMS
and LoRa paths remain independent.

### Validate retrieval before comparing LLMs

The RAG service explicitly uses the configured embedding model for both corpus
indexing and query vectors. Its Chroma collection and local embedding cache are
versioned by model identity, so switching models builds a compatible index while
leaving the prior index intact.

Load the complete Summit Connect corpus, then measure retrieval independently of
the LLM and message transport:

```bash
make corpus-load
make test-retrieval-evaluation
```

The retrieval gate reports evidence recall at 3, mean reciprocal rank, and p50/
p95 latency. Run `make test-evaluation` separately for end-to-end answer quality;
this distinction makes it clear whether a miss came from retrieval or generation.

For field rollout, package each event corpus as an immutable, optionally signed
OCI image. Event/version identity, integrity checks, rollback behavior, and the
OpenShift promotion workflow are documented in
[docs/corpus-packaging.md](docs/corpus-packaging.md).

The complete connected Big EVY sourcing, governance, evaluation, distribution,
and Lil EVY activation plan is saved in
[docs/big-evy-corpus-factory-roadmap.md](docs/big-evy-corpus-factory-roadmap.md).
The executable source-registry and bounded HTTPS acquisition contract is
documented in [docs/corpus-acquisition.md](docs/corpus-acquisition.md).

The first factory/runtime contract is now executable: allowlisted files become
immutable evidence snapshots, provenance-linked canonical documents, and
deterministic chunks; five independent promotion layers bind to the exact
release/model/retrieval identities; and Lil EVY stages, verifies, tests, and
atomically activates a signed corpus without lowering its anti-rollback floor.
The RAG runtime can now boot directly from that durable active pointer, reverify
the selected signed package, expose a bounded activation status, and fail closed
instead of falling back to unrelated local data.
Field responses also carry internal, text-free attribution to the exact active
corpus and selected evidence, while signed exceptional recovery remains bound to
the live node state and suppresses restricted or unclassified recovery evidence.
Fleet inventory now distinguishes active, recovery, unready, unknown, and drifted
nodes without claiming compliance until a desired release is configured.
For OpenShift lab testing, the chart can also enable an internal-only activation
sidecar. Its credentials are projected from Secrets, a 202 response still
requires a RAG pod restart, and exact digest/sequence reconciliation prevents an
accepted package from being mistaken for the release currently serving answers.
The next control layer is also executable as a crash-resumable state machine:
restart requests are idempotent, deadlines survive controller restarts, and fleet
compliance requires live proof of the exact production digest and sequence.
The opt-in chart profile now runs that controller autonomously with a projected,
controller-only Kubernetes identity and permission to patch only its own RAG
Deployment, making the complete cutover testable on OpenShift without field hardware.

The factory is domain-agnostic, but promotion is not. Each deployment supplies
an event policy that names the accepted authorities, required operational facts,
scope intersections, freshness limits, independent-source minimums, human
approvals, and safe no-answer behavior. The resulting suitability report is an
input to release promotion, so packaging success cannot hide missing or stale
field information. See
[docs/corpus-suitability.md](docs/corpus-suitability.md).

```bash
make test-corpus-factory
make test-connected-acquisition
make test-corpus-audit
make test-corpus-suitability
python scripts/acquire_corpus_source.py --help
python scripts/plan_corpus_refresh.py --help
python scripts/corpus_audit.py --help
python scripts/evaluate_corpus_suitability.py --help
python scripts/evaluate_corpus_release.py --help
```

These are connected-lab building blocks, not permission to onboard live crisis
data. Source authority, licensing, local-language review, production signing,
and release approval remain human-controlled gates.

## Micronode Footprint

Simulates an 8-core / 16 GB edge board (Orange Pi 5 Plus, Rock 5B, Intel NUC Edge class):

| Service | CPU | Memory | Role |
|---------|-----|--------|------|
| BitNet Server | 4.0 | 4 GB | LLM inference — `--threads 8 --ctx-size 512` |
| ChromaDB | 1.0 | 2 GB | Vector store + ONNX MiniLM embeddings |
| RAG Service | 0.5 | 2 GB | Hybrid search, RAG-direct fallback |
| Message Router | 0.5 | 512 MB | Classify → RAG-direct or LLM → respond |
| SMS Gateway | 0.5 | 512 MB | SMS receive/send + Redis Streams |
| LLM Inference | 0.5 | 512 MB | BitNet server wrapper |
| API Gateway | 0.25 | 256 MB | Service routing + metrics |
| Redis | 0.25 | 256 MB | Event stream + message queue |
| Privacy Filter | 0.25 | 256 MB | PII detection, rate limiting |

**Total: ~7.75 CPU, ~10.5 GB RAM** — fits on an 8-core / 16 GB edge board with OS headroom.

**RAG-direct fallback**: High-confidence corpus matches (score >= 0.8, under 160 chars) return instantly without calling the LLM. Queries like "WiFi password?", "where's lunch?", "emergency contact?" resolve in < 1 second.

## Scaling

One node handles local SMS traffic. To scale, deploy more nodes. Each node is fully self-contained — inference engine, knowledge base, SMS interface. No inter-node communication required for basic operation.

In the field: truck nodes to the affected area, power them up, they start serving immediately (zero-touch via RHEL image-mode). RHACM provides fleet visibility and pushes model/corpus updates when connectivity is available.

```
       RHACM Hub (when connected)
            │
    ┌───────┼───────┐
    ▼       ▼       ▼
  Node 1  Node 2  Node N
  (Site A) (Site B) (Site Z)
```

## Project Structure

```
├── backend/
│   ├── Containerfile              # UBI9-based slim container
│   ├── Containerfile.rag          # UBI9 + CPU-only PyTorch + OpenVINO
│   ├── api_gateway/               # Service routing + metrics
│   ├── shared/                    # Config, Pydantic models
│   └── services/
│       ├── sms_gateway/           # SMS simulation + Twilio stub
│       ├── message_router/        # Classify → RAG → LLM → respond
│       ├── llm_inference/         # Hardware-neutral model-provider adapter
│       ├── rag_service/           # ChromaDB + OpenVINO embeddings
│       └── privacy_filter/        # PII detection, rate limiting
├── data/summit_connect/           # RAG knowledge corpus
├── scripts/                       # send_sms.sh, build_corpus, demo_setup
├── tests/                         # CDD/TDD/EDD/BDD validation matrix
├── docs/architecture.md           # Full architecture document
├── docker-compose.yml             # Dev: single edge node
└── chart/                         # Helm chart for MicroShift (TODO)
```

## Based On

- [EVY](https://github.com/srex-dev/EVY) — SMS-based AI platform for off-grid edge deployment
- [Edge AI CPU Inference](https://github.com/jkershawrh/edge-ai-cpu-inference) — BitNet 1.58-bit inference quickstart for Red Hat OpenShift

## License

Apache 2.0
