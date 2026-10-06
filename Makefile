# Edge Inference at Scale — Makefile
# CDD → TDD → EDD → BDD: Gated validation matrix
# Run: make test-all

PROJECT ?= edge-inference-at-scale
PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest
HELM ?= helm
PODMAN ?= podman

.PHONY: help test-all test-contracts test-corpus-factory test-connected-acquisition test-corpus-audit test-corpus-suitability test-unit test-integration test-benchmarks \
        test-evaluation test-retrieval-evaluation test-bdd test-capacity test-capacity-live test-publication \
        lint build compose-up compose-down scale-up scale-down dashboard deploy

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

# ── Stage 0: Contracts (CDD) ──────────────────────────────────────────
test-contracts: ## Stage 0 — Validate API and corpus contracts
	$(PYTEST) tests/contracts/ tests/corpus_factory/ -v --tb=short

test-corpus-factory: ## Validate Big EVY contracts, factory core, and promotion gates
	$(PYTEST) tests/corpus_factory/ -v --tb=short

test-connected-acquisition: ## Validate source registry and bounded HTTPS acquisition
	$(PYTEST) tests/corpus_factory/test_connected_acquisition.py -v --tb=short

test-corpus-audit: ## Validate refresh planning and tamper-evident audit chain
	$(PYTEST) tests/corpus_factory/test_refresh_planning.py tests/corpus_factory/test_audit_ledger.py -v --tb=short

test-corpus-suitability: ## Prove the synthetic event is incomplete-then-corrected
	$(PYTEST) tests/integration/test_synthetic_suitability_drill.py -v --tb=short

# ── Stage 1: Unit (TDD) ──────────────────────────────────────────────
test-unit: ## Stage 1 — Unit tests (no external services)
	$(PYTEST) tests/unit/ -v --tb=short

# ── Stage 2: Integration ─────────────────────────────────────────────
test-integration: ## Stage 2 — Pipeline integration tests
	$(PYTEST) tests/integration/ -v --tb=short

# ── Stage 3: Evaluation (EDD) ────────────────────────────────────────
test-evaluation: ## Stage 3 — Response quality evaluation (requires live API)
	$(PYTHON) tests/evaluation/run_eval.py

test-retrieval-evaluation: ## Stage 3 — RAG recall, rank, and latency (requires live RAG)
	$(PYTHON) tests/evaluation/run_retrieval_eval.py

# ── Stage 3b: Capacity & Burst Benchmarks ────────────────────────────
test-capacity: ## Stage 3b — Capacity tests (mocked, runs in CI)
	$(PYTEST) tests/benchmarks/test_capacity.py -v --tb=short

test-capacity-live: ## Stage 3b — Capacity tests against live stack (needs docker compose up)
	CAPACITY_LIVE=1 $(PYTEST) tests/benchmarks/test_capacity.py::TestLiveCapacity -v --tb=short

# ── Stage 4: BDD ────────────────────────────────────────────────────
test-bdd: ## Stage 4 — BDD user scenarios (includes treasure hunt)
	$(PYTEST) tests/bdd/ -v --tb=short

test-benchmarks: ## Stage 4 — All benchmarks (unit + capacity)
	$(PYTEST) tests/benchmarks/ -v --tb=short

# ── Stage 5: Publication ─────────────────────────────────────────────
test-publication: ## Stage 5 — README and repo validation
	$(PYTEST) tests/publication/ -v --tb=short

# ── Aggregates ────────────────────────────────────────────────────────
test: ## Quick test — unit tests only
	$(PYTEST) tests/unit/ -q

test-all: ## Run all gated stages sequentially
	@echo "╔══════════════════════════════════════════╗"
	@echo "║  $(PROJECT) — Validation Matrix          ║"
	@echo "╚══════════════════════════════════════════╝"
	@$(MAKE) test-contracts   && echo "Stage 0: Contracts    ✅" || (echo "Stage 0: Contracts    ❌" && exit 1)
	@$(MAKE) test-unit        && echo "Stage 1: Unit/TDD     ✅" || (echo "Stage 1: Unit/TDD     ❌" && exit 1)
	@$(MAKE) test-integration && echo "Stage 2: Integration  ✅" || (echo "Stage 2: Integration  ❌" && exit 1)
	@$(MAKE) test-capacity    && echo "Stage 3b: Capacity    ✅" || (echo "Stage 3b: Capacity    ❌" && exit 1)
	@$(MAKE) test-bdd         && echo "Stage 4: BDD          ✅" || (echo "Stage 4: BDD          ❌" && exit 1)
	@$(MAKE) test-publication && echo "Stage 5: Publication  ✅" || (echo "Stage 5: Publication  ❌" && exit 1)
	@echo ""
	@echo "ALL STAGES GREEN ✅"

# ── Lint ──────────────────────────────────────────────────────────────
lint: ## Lint Python and Helm
	$(PYTHON) -m compileall -q backend corpus_factory scripts tests
	$(HELM) lint chart/
	@echo "Lint complete"

# ── Build ─────────────────────────────────────────────────────────────
build: ## Build container images
	$(PODMAN) compose build

build-rag: ## Build RAG service image (includes PyTorch + OpenVINO)
	$(PODMAN) build -t $(PROJECT)_rag-service -f backend/Containerfile.rag backend/

# ── Run ───────────────────────────────────────────────────────────────
compose-up: ## Start single edge node
	$(PODMAN) compose up -d

compose-down: ## Stop single edge node
	$(PODMAN) compose down

scale-up: ## Start 3-node fleet
	$(PODMAN) compose -f docker-compose.scale.yml up -d

scale-down: ## Stop 3-node fleet
	$(PODMAN) compose -f docker-compose.scale.yml down

# ── Dashboard ─────────────────────────────────────────────────────────
dashboard: ## Launch metrics dashboard
	@echo "Dashboard at http://localhost:8888?api=http://localhost:8000"
	cd dashboard && $(PYTHON) serve.py

# ── Deploy ────────────────────────────────────────────────────────────
deploy: ## Deploy to OpenShift via Helm
	$(HELM) upgrade --install edge-inference chart/ \
		--set backend.image=image-registry.openshift-image-registry.svc:5000/edge-inference/edge-backend:latest \
		--set rag.image=image-registry.openshift-image-registry.svc:5000/edge-inference/edge-rag:latest \
		--set bitnet.image=image-registry.openshift-image-registry.svc:5000/edge-inference/bitnet-cpp:latest

# ── Corpus ────────────────────────────────────────────────────────────
corpus-load: ## Load Summit Connect corpus into RAG service
	$(PYTHON) scripts/build_summit_corpus.py

# ── SMS ───────────────────────────────────────────────────────────────
sms: ## Send a test SMS (usage: make sms MSG="your question")
	./scripts/send_sms.sh "$(MSG)"
