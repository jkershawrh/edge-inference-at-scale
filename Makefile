# Edge Inference at Scale — Makefile
# CDD → TDD → EDD → BDD: Gated validation matrix
# Run: make test-all

PROJECT ?= edge-inference-at-scale
PYTHON ?= python3
PYTEST ?= $(PYTHON) -m pytest
HELM ?= helm
PODMAN ?= podman

.PHONY: help test-all test-contracts test-unit test-integration test-benchmarks \
        test-evaluation test-publication lint build compose-up compose-down \
        scale-up scale-down dashboard deploy

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

# ── Stage 0: Contracts (CDD) ──────────────────────────────────────────
test-contracts: ## Stage 0 — Validate API contracts
	$(PYTEST) tests/contracts/ -v --tb=short

# ── Stage 1: Unit (TDD) ──────────────────────────────────────────────
test-unit: ## Stage 1 — Unit tests (143 tests, no external deps)
	$(PYTEST) tests/unit/ -v --tb=short

# ── Stage 2: Integration ─────────────────────────────────────────────
test-integration: ## Stage 2 — Pipeline integration tests
	$(PYTEST) tests/integration/ -v --tb=short

# ── Stage 3: Evaluation (EDD) ────────────────────────────────────────
test-evaluation: ## Stage 3 — Response quality evaluation (requires live API)
	$(PYTHON) tests/evaluation/run_eval.py

# ── Stage 4: Benchmarks (BDD) ────────────────────────────────────────
test-benchmarks: ## Stage 4 — Performance benchmarks
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
	@$(MAKE) test-benchmarks  && echo "Stage 4: Benchmarks   ✅" || (echo "Stage 4: Benchmarks   ❌" && exit 1)
	@$(MAKE) test-publication && echo "Stage 5: Publication  ✅" || (echo "Stage 5: Publication  ❌" && exit 1)
	@echo ""
	@echo "ALL STAGES GREEN ✅"

# ── Lint ──────────────────────────────────────────────────────────────
lint: ## Lint Python and Helm
	$(PYTHON) -c "import ast, os, sys; \
		errs = 0; \
		[exec('try:\n ast.parse(open(os.path.join(r,f)).read())\nexcept SyntaxError as e:\n print(f\"  FAIL: {os.path.join(r,f)}: {e}\"); errs += 1', {'ast':ast,'os':os,'open':open,'print':print,'SyntaxError':SyntaxError,'errs':errs}) for r,_,fs in os.walk('backend') for f in fs if f.endswith('.py')]; \
		print(f'Python syntax: {\"PASS\" if not errs else \"FAIL\"}')"
	$(HELM) lint chart/ 2>/dev/null || true
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
