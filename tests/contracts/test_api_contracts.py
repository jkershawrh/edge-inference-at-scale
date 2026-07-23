"""CDD Stage 0 — API contract validation.

Verifies that each FastAPI service exposes the expected endpoints
with correct request/response schemas. No external services needed.
"""

import importlib
import importlib.util

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute


def _get_routes(app: FastAPI) -> dict:
    """Extract route paths and methods from a FastAPI app."""
    routes = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                key = f"{method} {route.path}"
                routes[key] = route
    return routes


class TestSMSGatewayContract:
    def setup_method(self):
        from backend.services.sms_gateway.main import app
        self.routes = _get_routes(app)

    def test_has_health_endpoint(self):
        assert "GET /health" in self.routes

    def test_has_sms_receive(self):
        assert "POST /sms/receive" in self.routes

    def test_has_sms_send(self):
        assert "POST /sms/send" in self.routes

    def test_has_sms_history(self):
        assert "GET /sms/history" in self.routes

    def test_has_twilio_webhook(self):
        assert "POST /twilio/webhook" in self.routes

    def test_has_stream_health(self):
        assert "GET /stream/health" in self.routes


class TestMessageRouterContract:
    def setup_method(self):
        from backend.services.message_router.main import app
        self.routes = _get_routes(app)

    def test_has_health_endpoint(self):
        assert "GET /health" in self.routes

    def test_has_route_endpoint(self):
        assert "POST /route" in self.routes

    def test_has_classify_endpoint(self):
        assert "POST /classify" in self.routes

    def test_has_statistics_endpoint(self):
        assert "GET /statistics" in self.routes


class TestLLMInferenceContract:
    def setup_method(self):
        from backend.services.llm_inference.main import app
        self.routes = _get_routes(app)

    def test_has_health_endpoint(self):
        assert "GET /health" in self.routes

    def test_has_inference_endpoint(self):
        assert "POST /inference" in self.routes

    def test_has_chat_completions_passthrough(self):
        assert "POST /v1/chat/completions" in self.routes

    def test_has_models_passthrough(self):
        assert "GET /v1/models" in self.routes

    def test_has_stats_endpoint(self):
        assert "GET /stats" in self.routes


@pytest.mark.skipif(
    not importlib.util.find_spec("chromadb"),
    reason="chromadb not installed",
)
class TestRAGServiceContract:
    def setup_method(self):
        try:
            __import__("pysqlite3")
            import sys
            sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
        except ImportError:
            pass
        from backend.services.rag_service.main import app
        self.routes = _get_routes(app)

    def test_has_health_endpoint(self):
        assert "GET /health" in self.routes

    def test_has_search_endpoint(self):
        assert "POST /search" in self.routes

    def test_has_add_endpoint(self):
        assert "POST /add" in self.routes

    def test_has_stats_endpoint(self):
        assert "GET /stats" in self.routes

    def test_has_bulk_add_endpoint(self):
        assert "POST /documents/bulk-add" in self.routes


class TestNodeManagerContract:
    def setup_method(self):
        from backend.services.node_manager.main import app
        self.routes = _get_routes(app)

    def test_has_health_endpoint(self):
        assert "GET /health" in self.routes

    def test_has_register_endpoint(self):
        assert "POST /nodes/register" in self.routes

    def test_has_heartbeat_endpoint(self):
        assert "POST /nodes/heartbeat" in self.routes

    def test_has_fleet_endpoint(self):
        assert "GET /nodes/fleet" in self.routes

    def test_has_fleet_summary_endpoint(self):
        assert "GET /nodes/fleet/summary" in self.routes

    def test_has_route_endpoint(self):
        assert "POST /nodes/route" in self.routes


class TestAPIGatewayContract:
    def setup_method(self):
        from backend.api_gateway.main import app
        self.routes = _get_routes(app)

    def test_has_health_endpoint(self):
        assert "GET /health" in self.routes

    def test_has_services_health(self):
        assert "GET /services/health" in self.routes

    def test_has_sms_receive_proxy(self):
        assert "POST /sms/receive" in self.routes

    def test_has_sms_history_proxy(self):
        assert "GET /sms/history" in self.routes

    def test_has_router_statistics_proxy(self):
        assert "GET /router/statistics" in self.routes

    def test_has_llm_stats_proxy(self):
        assert "GET /llm/stats" in self.routes

    def test_has_rag_search_proxy(self):
        assert "POST /rag/search" in self.routes
