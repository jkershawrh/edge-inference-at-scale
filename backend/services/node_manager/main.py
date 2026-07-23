"""Node Manager — Fleet orchestrator for Edge Inference at Scale."""
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.shared.models import SMSMessage, ServiceHealth

logger = logging.getLogger("node-manager")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

HEARTBEAT_TIMEOUT_SECONDS = 60


# --- Request / Response Models ---

class NodeRegistration(BaseModel):
    node_id: str
    api_url: str
    capabilities: Dict[str, Any] = {}


class NodeHeartbeat(BaseModel):
    node_id: str
    metrics: Dict[str, Any] = {}


class RouteRequest(BaseModel):
    message: SMSMessage


class RouteResponse(BaseModel):
    routed_to: str
    api_url: str
    status_code: int
    response: Optional[Dict[str, Any]] = None


class FleetSummary(BaseModel):
    total_nodes: int
    online_nodes: int
    total_messages_processed: int
    avg_latency_ms: float
    total_rag_direct: int


# --- Node Manager ---

class NodeManager:
    """Manages a fleet of edge inference nodes."""

    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.client: Optional[httpx.AsyncClient] = None

    async def initialize(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        logger.info("NodeManager initialized")

    async def shutdown(self):
        if self.client:
            await self.client.aclose()
        logger.info("NodeManager shut down")

    def register_node(self, node_id: str, api_url: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        self.nodes[node_id] = {
            "node_id": node_id,
            "api_url": api_url,
            "capabilities": capabilities,
            "registered_at": now,
            "last_seen": now,
            "metrics": {},
            "status": "online",
        }
        logger.info("Registered node %s at %s", node_id, api_url)
        return self.nodes[node_id]

    def heartbeat(self, node_id: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
        if node_id not in self.nodes:
            raise KeyError(f"Node {node_id} is not registered")
        self.nodes[node_id]["last_seen"] = time.time()
        self.nodes[node_id]["metrics"] = metrics
        self.nodes[node_id]["status"] = "online"
        logger.debug("Heartbeat from %s: %s", node_id, metrics)
        return self.nodes[node_id]

    def _compute_status(self, node: Dict[str, Any]) -> str:
        elapsed = time.time() - node["last_seen"]
        return "online" if elapsed < HEARTBEAT_TIMEOUT_SECONDS else "offline"

    def get_fleet_status(self) -> List[Dict[str, Any]]:
        fleet = []
        for node_id, node in self.nodes.items():
            status = self._compute_status(node)
            fleet.append({
                "node_id": node_id,
                "api_url": node["api_url"],
                "capabilities": node["capabilities"],
                "status": status,
                "last_seen": node["last_seen"],
                "metrics": node["metrics"],
            })
        return fleet

    def get_fleet_summary(self) -> Dict[str, Any]:
        fleet = self.get_fleet_status()
        total_nodes = len(fleet)
        online_nodes = sum(1 for n in fleet if n["status"] == "online")

        total_messages = 0
        total_latency = 0.0
        latency_count = 0
        total_rag_direct = 0

        for node in fleet:
            m = node.get("metrics", {})
            total_messages += m.get("messages_received", 0)
            if "avg_latency_ms" in m:
                total_latency += m["avg_latency_ms"]
                latency_count += 1
            total_rag_direct += m.get("rag_direct", 0)

        avg_latency = total_latency / latency_count if latency_count > 0 else 0.0

        return {
            "total_nodes": total_nodes,
            "online_nodes": online_nodes,
            "total_messages_processed": total_messages,
            "avg_latency_ms": round(avg_latency, 2),
            "total_rag_direct": total_rag_direct,
        }

    async def route_to_best_node(self, message: SMSMessage) -> Dict[str, Any]:
        online_nodes = [
            n for n in self.nodes.values()
            if self._compute_status(n) == "online"
        ]
        if not online_nodes:
            raise RuntimeError("No online nodes available for routing")

        # Pick node with lowest load (messages_received from heartbeat metrics)
        best = min(
            online_nodes,
            key=lambda n: n.get("metrics", {}).get("messages_received", 0),
        )
        node_id = best["node_id"]
        api_url = best["api_url"]

        logger.info("Routing message to %s (%s)", node_id, api_url)
        try:
            resp = await self.client.post(
                f"{api_url}/sms/receive",
                json=message.model_dump(mode="json"),
            )
            return {
                "routed_to": node_id,
                "api_url": api_url,
                "status_code": resp.status_code,
                "response": resp.json() if resp.status_code == 200 else None,
            }
        except httpx.ConnectError:
            logger.error("Failed to reach node %s at %s", node_id, api_url)
            raise RuntimeError(f"Node {node_id} unreachable at {api_url}")


# --- Application ---

manager = NodeManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Node Manager starting up...")
    await manager.initialize()
    yield
    logger.info("Node Manager shutting down...")
    await manager.shutdown()


app = FastAPI(
    title="Edge Inference at Scale - Node Manager",
    description="Fleet orchestrator for multi-node edge inference",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Endpoints ---

@app.get("/health")
async def health_check():
    fleet = manager.get_fleet_status()
    online = sum(1 for n in fleet if n["status"] == "online")
    return ServiceHealth(
        service_name="node-manager",
        status="healthy",
        version="1.0.0",
        details={
            "total_nodes": len(fleet),
            "online_nodes": online,
        },
    )


@app.post("/nodes/register")
async def register_node(registration: NodeRegistration):
    node = manager.register_node(
        node_id=registration.node_id,
        api_url=registration.api_url,
        capabilities=registration.capabilities,
    )
    return {"status": "registered", "node": node}


@app.post("/nodes/heartbeat")
async def node_heartbeat(hb: NodeHeartbeat):
    try:
        node = manager.heartbeat(node_id=hb.node_id, metrics=hb.metrics)
        return {"status": "ok", "node_id": hb.node_id}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/nodes/fleet")
async def fleet_status():
    return manager.get_fleet_status()


@app.get("/nodes/fleet/summary")
async def fleet_summary():
    summary = manager.get_fleet_summary()
    return FleetSummary(**summary)


@app.post("/nodes/route")
async def route_message(req: RouteRequest):
    try:
        result = await manager.route_to_best_node(req.message)
        return RouteResponse(**result)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8006, log_level="info")
