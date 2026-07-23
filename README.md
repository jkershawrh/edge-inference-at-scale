# Edge Inference at Scale

**SMS → LLM → SMS** | AI at the edge, powered by **Red Hat** + **Intel**

> In a warzone, disaster zone, or underserved community — anywhere 2G cellular works — people can text in and get knowledge back. No internet. No app. No GPU. Just SMS and a 1-bit language model running on CPU.

## What This Is

A reference architecture and live demo showing how to deploy LLM inference at the edge with minimal resources. Users send SMS messages and receive AI-generated responses powered by a **BitNet 1.58-bit ternary model** (~400MB, CPU-only) with **Retrieval-Augmented Generation** for domain-specific knowledge.

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
  │                        (OpenVINO +    (BitNet b1.58           │
  │                         MiniLM)       via llama.cpp)          │
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
| **LLM** | BitNet b1.58-2B-4T via llama.cpp | 1-bit ternary weights → integer-only math → no GPU needed |
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
curl http://localhost:8000/router/statistics   # Message throughput
```

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
│       ├── llm_inference/         # BitNet server wrapper
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
