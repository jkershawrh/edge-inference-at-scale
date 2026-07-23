"""API Gateway — Main entry point for Edge Inference at Scale."""
import logging
import os

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import Dict

from backend.shared.models import SMSMessage, ServiceHealth
from backend.shared.config import settings

logger = logging.getLogger("api-gateway")


class APIGateway:
    def __init__(self):
        self.services = {
            "sms-gateway": os.getenv("SMS_GATEWAY_URL", settings.sms_gateway_url),
            "message-router": os.getenv("MESSAGE_ROUTER_URL", settings.message_router_url),
            "llm-inference": os.getenv("LLM_INFERENCE_URL", settings.llm_inference_url),
            "rag-service": os.getenv("RAG_SERVICE_URL", settings.rag_service_url),
            "privacy-filter": os.getenv("PRIVACY_FILTER_URL", settings.privacy_filter_url),
        }
        self.client: httpx.AsyncClient | None = None

    async def initialize(self):
        self.client = httpx.AsyncClient(timeout=15.0)

    async def shutdown(self):
        if self.client:
            await self.client.aclose()

    async def check_all_services(self) -> Dict[str, Dict]:
        results = {}
        for service_name, service_url in self.services.items():
            try:
                response = await self.client.get(f"{service_url}/health", timeout=5.0)
                if response.status_code == 200:
                    results[service_name] = {"status": "healthy", "details": response.json()}
                else:
                    results[service_name] = {"status": "unhealthy", "code": response.status_code}
            except Exception as e:
                results[service_name] = {"status": "unreachable", "error": str(e)}
        return results

    async def proxy(self, service: str, path: str, method: str = "GET", body: dict = None) -> dict:
        url = f"{self.services[service]}/{path.lstrip('/')}"
        try:
            if method == "GET":
                resp = await self.client.get(url)
            elif method == "POST":
                resp = await self.client.post(url, json=body)
            elif method == "DELETE":
                resp = await self.client.delete(url)
            else:
                raise HTTPException(status_code=405, detail=f"Method {method} not supported")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Service {service} unreachable")


gateway = APIGateway()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("API Gateway starting up...")
    await gateway.initialize()
    yield
    logger.info("API Gateway shutting down...")
    await gateway.shutdown()


app = FastAPI(
    title="Edge Inference at Scale - API Gateway",
    description="Gateway for SMS-based edge AI inference demo",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Health ---

@app.get("/health")
async def health_check():
    return ServiceHealth(
        service_name="api-gateway",
        status="healthy",
        version="1.0.0",
        details={"node_id": settings.node_id, "sms_mode": settings.sms_mode},
    )


@app.get("/services/health")
async def all_services_health():
    return await gateway.check_all_services()


# --- SMS Gateway ---

@app.post("/sms/receive")
async def receive_sms(message: SMSMessage):
    return await gateway.proxy("sms-gateway", "/sms/receive", "POST", message.model_dump(mode="json"))


@app.post("/sms/send")
async def send_sms(message: SMSMessage):
    return await gateway.proxy("sms-gateway", "/sms/send", "POST", {"phone_number": message.receiver, "content": message.content})


@app.get("/sms/history")
async def sms_history():
    return await gateway.proxy("sms-gateway", "/sms/history")


# --- Message Router ---

@app.post("/router/route")
async def route_message(message: SMSMessage):
    return await gateway.proxy("message-router", "/route", "POST", message.model_dump(mode="json"))


@app.get("/router/statistics")
async def router_statistics():
    return await gateway.proxy("message-router", "/statistics")


# --- LLM Inference ---

@app.get("/llm/health")
async def llm_health():
    return await gateway.proxy("llm-inference", "/health")


@app.get("/llm/stats")
async def llm_stats():
    return await gateway.proxy("llm-inference", "/stats")


# --- RAG Service ---

@app.post("/rag/search")
async def rag_search(query: dict):
    return await gateway.proxy("rag-service", "/search", "POST", query)


@app.get("/rag/stats")
async def rag_stats():
    return await gateway.proxy("rag-service", "/stats")


@app.post("/rag/add")
async def rag_add_document(doc: dict):
    return await gateway.proxy("rag-service", "/add", "POST", doc)


# --- Privacy Filter ---

@app.post("/privacy/validate")
async def validate_message(message: SMSMessage):
    return await gateway.proxy("privacy-filter", "/validate", "POST", message.model_dump(mode="json"))


@app.get("/privacy/stats")
async def privacy_stats():
    return await gateway.proxy("privacy-filter", "/stats")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.api_gateway_port, log_level="info")
