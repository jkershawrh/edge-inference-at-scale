# Edge Inference at Scale — Architecture

## The Story

Anywhere 2G cellular works — warzones, disaster zones, underserved communities — people can text in and get knowledge back. No internet required. No app to install. No GPU. Just SMS and a 1-bit language model running on CPU at the edge.

This reference architecture demonstrates how **Red Hat** and **Intel** technologies combine to make edge AI inference practical, scalable, and deployable in the real world.

## Architecture Tiers

```
┌─────────────────────────────────────────────────────────────────────┐
│                      MANAGEMENT PLANE                              │
│                                                                     │
│   RHACM (Red Hat Advanced Cluster Management)                      │
│   ├── Fleet visibility — all edge nodes, all locations             │
│   ├── GitOps policy push — model updates, corpus updates           │
│   └── Zero-touch provisioning — new nodes boot and join fleet      │
│                                                                     │
│   Observability Stack                                               │
│   ├── Prometheus / Thanos — metrics from all nodes                 │
│   ├── Grafana dashboards — inference latency, throughput, health   │
│   └── Alerting — node down, model drift, queue depth               │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ GitOps sync / metrics push
                               │ (when connectivity available)
┌──────────────────────────────▼──────────────────────────────────────┐
│                         EDGE NODE (x N)                            │
│                                                                     │
│   ┌─────────────────────────────────────────────────────────────┐  │
│   │  RHEL Image Mode (bootc)                                    │  │
│   │  ├── Immutable OS image — atomic updates, safe rollbacks    │  │
│   │  ├── Pre-baked container images — works fully offline       │  │
│   │  └── Zero-touch — boots, joins fleet, starts serving        │  │
│   │                                                             │  │
│   │  MicroShift (lightweight OpenShift)                         │  │
│   │  ├── Single-node Kubernetes — same APIs as OpenShift        │  │
│   │  ├── ~800MB RAM overhead                                    │  │
│   │  ├── GitOps agent for app lifecycle                         │  │
│   │  └── Multus for operational networks (sensor nets, GSM)     │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                                                                     │
│   ┌─── Workloads (Pods on MicroShift) ──────────────────────────┐  │
│   │                                                              │  │
│   │   SMS Gateway ──► Message Router                             │  │
│   │   (event stream)      │         │                            │  │
│   │        ▲              ▼         ▼                            │  │
│   │        │        RAG Service   LLM Inference                  │  │
│   │   SMS Out      (OpenVINO      (BitNet b1.58                  │  │
│   │                 embeddings)    via llama.cpp)                 │  │
│   │                                                              │  │
│   │   Privacy Filter    Redis Streams    ChromaDB                │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│   ┌─── Intel Hardware ──────────────────────────────────────────┐  │
│   │   Intel Xeon 6 (Granite Rapids)                              │  │
│   │   ├── AVX-512 / VNNI — BitNet ternary kernel acceleration   │  │
│   │   ├── OpenVINO runtime — MiniLM embedding inference (INT8)  │  │
│   │   ├── AMX (optional) — larger model inference if needed     │  │
│   │   └── TDX (optional) — confidential AI for sensitive data   │  │
│   └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## Technology Mapping

### Red Hat Stack

| Technology | Role | Why |
|-----------|------|-----|
| **RHEL Image Mode (bootc)** | Immutable OS for edge nodes | Container-native OS delivery. Nodes boot from a pre-built image containing MicroShift + all workload containers. No internet needed at boot. Atomic updates with safe rollback. |
| **MicroShift** | Lightweight Kubernetes on each node | Single-binary Kubernetes derived from OpenShift. Runs on ~800MB RAM. Same APIs/tooling as full OpenShift — Helm charts, pod specs, services all work. GitOps agent for app updates. |
| **RHACM** | Fleet management | Manages hundreds/thousands of edge nodes from a central hub. Pushes policies, model updates, and corpus refreshes via GitOps. Zero-touch provisioning for new nodes. |
| **UBI9** | Container base images | Red Hat Universal Base Image — security-hardened, CVE-scanned, supported. All workload containers built on UBI9-minimal. |

### Intel Stack

| Technology | Role | Why |
|-----------|------|-----|
| **AVX-512 / VNNI** | BitNet ternary inference acceleration | BitNet 1.58-bit weights are ternary ({-1, 0, 1}). Matrix multiplies become integer add/subtract. AVX-512 VNNI instructions accelerate these integer operations — no floating point, no GPU needed. |
| **OpenVINO** | RAG embedding inference | `sentence-transformers` supports OpenVINO backend natively (`backend="openvino"`). MiniLM-L6-v2 embeddings run 2-3x faster on Intel CPUs with INT8 quantization via OpenVINO vs. default PyTorch. |
| **AMX** | Heavier model inference (optional) | Advanced Matrix Extensions on Xeon 6 accelerate INT8/BF16 matrix operations. For scenarios needing a larger model (7B+), AMX delivers up to 14x better inference vs. baseline. Each core: 2,048 INT8 ops/cycle. |
| **TDX** | Confidential AI (optional) | Trust Domain Extensions provide hardware-isolated VMs with encrypted memory. For edge deployments handling sensitive data (medical triage, warzone intelligence), TDX ensures data confidentiality even if the node is physically compromised. |

### Why This Combination

The key insight: **you don't need a GPU to run useful AI inference at the edge.** BitNet's 1-bit ternary weights turn matrix multiplication into integer addition. Intel's AVX-512 instructions are built for exactly this. A single Xeon core handles thousands of integer operations per cycle. The model fits in ~400MB of RAM.

Red Hat's edge stack (RHEL image-mode + MicroShift) makes this deployable at scale — nodes boot from images, join the fleet automatically, and receive updates via GitOps. No operator needed on-site.

## Workload Architecture

### SMS Message Flow

```
User sends SMS (2G cellular)
    │
    ▼
SMS Gateway (receives via GSM modem or Twilio webhook)
    │
    ├──► Redis Streams (event stream — ordered, persistent, backpressure)
    │
    ▼
Message Router (consumes from stream)
    │
    ├── Classify intent (emergency / command / query)
    │
    ├── [if query] ──► RAG Service
    │                   ├── Embed query (OpenVINO + MiniLM)
    │                   ├── Vector search (ChromaDB)
    │                   └── Return top-k context documents + scores
    │
    ├── [if RAG score >= 0.8 and doc <= 160 chars]
    │       └── RAG-direct: return corpus doc as SMS (skip LLM, < 1s)
    │
    ├── [else] ──► LLM Inference
    │                   ├── System prompt + RAG context + user query
    │                   ├── BitNet b1.58-2B-4T via llama-server
    │                   ├── AVX-512 accelerated integer inference
    │                   └── Response truncated to 160 chars (SMS limit)
    │
    └── Privacy Filter (PII detection, rate limiting)
         │
         ▼
    SMS Gateway sends response back via SMS
```

### Event Stream vs HTTP

The current implementation uses HTTP forwarding between services (EVY pattern). For production edge deployment, this should evolve to **Redis Streams**:

| Aspect | HTTP Forwarding (current) | Redis Streams (production) |
|--------|--------------------------|---------------------------|
| Delivery | At-most-once | At-least-once with consumer groups |
| Backpressure | Queue depth + 429 errors | Stream length limits + consumer lag |
| Ordering | Not guaranteed | Guaranteed within stream |
| Persistence | In-memory only | AOF/RDB persistence |
| Multi-consumer | No | Yes — fan-out to multiple processors |
| Offline resilience | Messages lost on restart | Messages survive restart |

Redis Streams are viable at the edge because Redis itself runs with ~50MB RAM. The stream provides ordered, persistent message delivery without the overhead of Kafka or a full message broker.

## Node Profiles

### Micronode (Simulated Edge Board) — The Demo

Simulates an 8-core / 16 GB edge board (Orange Pi 5 Plus, Rock 5B, Intel NUC Edge class). This is what we deploy for the Summit Connect live demo.

| Resource | Allocation | Rationale |
|----------|-----------|-----------|
| CPU | 8 cores total | BitNet gets 4, ChromaDB gets 1, services share the rest |
| RAM | 16 GB total | BitNet model (~400MB) + ChromaDB ONNX (~200MB) + services + OS |
| Storage | 20 GB | OS + containers + RAG corpus + Redis AOF |
| Network | 2G cellular (GSM) or ethernet | SMS in/out |
| Power | 25-45W | Solar viable for field deployment |

**Service allocation within the micronode:**

| Service | CPU | Memory |
|---------|-----|--------|
| BitNet Server | 4.0 | 4 Gi |
| ChromaDB | 1.0 | 2 Gi |
| RAG Service | 0.5 | 2 Gi |
| Message Router | 0.5 | 512 Mi |
| SMS Gateway | 0.5 | 512 Mi |
| LLM Inference | 0.5 | 512 Mi |
| API Gateway | 0.25 | 256 Mi |
| Redis | 0.25 | 256 Mi |
| Privacy Filter | 0.25 | 256 Mi |
| **Total** | **7.75** | **10.5 Gi** |

**Expected performance:**
- RAG-direct response (high-confidence corpus match): **< 1 second**
- BitNet inference (open-ended question): **5-10 seconds** on dedicated hardware
- Throughput: **10-20 requests/minute** (BitNet), **hundreds/minute** (RAG-direct)

### lilEVY (Edge Node) — Original

| Resource | Allocation | Rationale |
|----------|-----------|-----------|
| CPU | 4-6 cores | BitNet gets 2, RAG/embeddings get 1, routing/gateway share the rest |
| RAM | 4-6 GB | BitNet model (~400MB) + embeddings (~200MB) + services + OS |
| Storage | 32 GB | OS image + container images + RAG corpus + Redis AOF |
| Network | 2G cellular (GSM) or ethernet | SMS in/out; management plane sync when available |
| Power | 15-50W | Solar viable for field deployment |

### bigEVY (Central Node) — Optional

For deployments that need heavier inference or centralized knowledge:

| Resource | Allocation | Rationale |
|----------|-----------|-----------|
| CPU | 16+ cores (Xeon 6 with AMX) | Runs 7B-13B models with AMX acceleration |
| RAM | 32-64 GB | Larger models, global RAG corpus |
| Storage | 1 TB+ | Full knowledge base, analytics, audit logs |
| Network | Internet-connected | Receives overflow from edge nodes, syncs knowledge |
| Acceleration | AMX for INT8/BF16 inference | Up to 2,048 INT8 ops/cycle per core |

### When to Use TDX

Add Intel TDX when the edge node processes:
- Medical/health data (HIPAA, GDPR)
- Military/intelligence data (classified environments)
- Financial data (PCI-DSS)
- Personal identification data in disaster contexts

TDX provides hardware-isolated VMs with per-TD AES-128 encrypted memory. The workloads run unchanged — TDX is transparent to the application layer. Remote attestation verifies the node's integrity before sensitive data is sent.

## Scaling Model

```
                    RHACM Hub
                   ┌─────────┐
                   │  Fleet   │
                   │ Manager  │
                   └────┬─────┘
                        │ GitOps
          ┌─────────────┼─────────────┐
          │             │             │
     ┌────▼────┐  ┌────▼────┐  ┌────▼────┐
     │ Node 1  │  │ Node 2  │  │ Node N  │
     │ (Site A)│  │ (Site B)│  │ (Site Z)│
     │ lilEVY  │  │ lilEVY  │  │ lilEVY  │
     └─────────┘  └─────────┘  └─────────┘
       GSM           GSM           GSM
       modem         modem         modem
```

Each node is **fully self-contained** — it processes SMS, runs inference, and returns responses independently. No inter-node communication required for basic operation. Nodes can operate indefinitely without connectivity to the management plane.

**Scaling = deploying more nodes**, not making one node bigger. Each node handles its local SMS traffic. In a disaster scenario, you truck nodes to the affected area, power them up, and they start serving immediately (zero-touch via RHEL image-mode).

The management plane (RHACM) provides:
- **Visibility**: which nodes are online, their health, inference metrics
- **Updates**: push new models, updated RAG corpus, configuration changes
- **Provisioning**: new nodes boot from the RHEL image and auto-register

## Deployment Artifacts

| Artifact | Format | Target |
|----------|--------|--------|
| Edge node OS | RHEL bootc image | Bare metal / VM with MicroShift embedded |
| Workloads | Helm chart | MicroShift (Kubernetes API) |
| BitNet server | Container image (UBI9) | Pod on MicroShift |
| Backend services | Container images (UBI9) | Pods on MicroShift |
| RAG corpus | JSON → ChromaDB | Loaded at first boot or via GitOps sync |
| Fleet policies | RHACM policies | Applied from central hub |

### Development Path (docker-compose)

For development and demo on any x86 machine:
```bash
docker compose up    # or podman-compose
./scripts/send_sms.sh "What sessions are about AI?"
```

### Production Path (MicroShift + RHEL image-mode)

For field deployment at scale:
```bash
# 1. Build the bootc image (includes MicroShift + all workload containers)
# 2. Flash to edge hardware (Intel Xeon-based)
# 3. Boot — node starts MicroShift, pulls workloads, starts serving
# 4. Register with RHACM hub for fleet management
```
