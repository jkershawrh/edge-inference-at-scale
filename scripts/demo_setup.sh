#!/usr/bin/env bash
set -euo pipefail

echo "=== Edge Inference at Scale — Demo Setup ==="
echo ""

# Start services
echo "[1/4] Starting services..."
docker compose up -d

# Wait for services to be healthy
echo "[2/4] Waiting for services to be healthy..."
echo "  (BitNet model loading takes ~60 seconds on first run)"

MAX_WAIT=180
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo "  API Gateway: healthy"
        break
    fi
    sleep 5
    WAITED=$((WAITED + 5))
    echo "  Waiting... (${WAITED}s)"
done

if [ $WAITED -ge $MAX_WAIT ]; then
    echo "ERROR: Services did not become healthy within ${MAX_WAIT}s"
    echo "Check logs: docker compose logs"
    exit 1
fi

# Load RAG corpus
echo "[3/4] Loading Summit Connect corpus into RAG..."
python3 scripts/build_summit_corpus.py || {
    echo "WARN: Corpus loading failed — RAG may use sample data only"
}

# Status
echo ""
echo "[4/4] Status check..."
echo ""
curl -s http://localhost:8000/services/health | python3 -m json.tool 2>/dev/null || echo "(install python3 for formatted output)"

echo ""
echo "=== Demo Ready ==="
echo ""
echo "  Web UI:     http://localhost:3000"
echo "  API:        http://localhost:8000"
echo "  BitNet:     http://localhost:8080"
echo ""
echo "  Send an SMS: open the Messages page and try:"
echo "    'What sessions are about edge computing?'"
echo "    'Where is the keynote?'"
echo "    'Best restaurants nearby?'"
echo ""
