#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Edge Inference at Scale — 3-Node Fleet Demo ==="
echo ""
echo "Starting 3 edge nodes + shared infrastructure..."
docker compose -f docker-compose.scale.yml up -d --build

echo ""
echo "Waiting for nodes to come online (this may take a few minutes)..."
sleep 30

echo ""
echo "Registering nodes with fleet manager..."
for i in 1 2 3; do
    node_id=$(printf "edge-node-%03d" "$i")
    curl -s -X POST http://localhost:8006/nodes/register \
        -H "Content-Type: application/json" \
        -d "{\"node_id\": \"${node_id}\", \"api_url\": \"http://node-${i}-gateway:8000\", \"capabilities\": {\"bitnet\": true, \"rag\": true}}" \
        | python3 -m json.tool
    echo ""
done

echo "Fleet status:"
curl -s http://localhost:8006/nodes/fleet | python3 -m json.tool
echo ""

echo "Fleet summary:"
curl -s http://localhost:8006/nodes/fleet/summary | python3 -m json.tool
echo ""

echo "=== Fleet is ready ==="
echo ""
echo "Send an SMS through the load balancer (round-robin):"
echo "  curl -X POST http://localhost:8000/sms/receive \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"sender\": \"+15551234567\", \"receiver\": \"+15559876543\", \"content\": \"What sessions are about AI?\"}'"
echo ""
echo "Send an SMS through the node manager (least-loaded routing):"
echo "  curl -X POST http://localhost:8006/nodes/route \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\": {\"sender\": \"+15551234567\", \"receiver\": \"+15559876543\", \"content\": \"What sessions are about AI?\"}}'"
echo ""
echo "Check fleet status:  curl -s http://localhost:8006/nodes/fleet | python3 -m json.tool"
echo "Fleet summary:       curl -s http://localhost:8006/nodes/fleet/summary | python3 -m json.tool"
echo "Node manager health: curl -s http://localhost:8006/health | python3 -m json.tool"
