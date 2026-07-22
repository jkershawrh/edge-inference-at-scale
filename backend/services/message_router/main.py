"""Edge Inference at Scale - Message Router Service.

Classifies incoming SMS messages and routes them through RAG and LLM
services, then delivers the response back via the SMS gateway.
Rebranded from EVY for Summit Connect.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.shared.config import settings
from backend.shared.models import (
    LLMRequest,
    LLMResponse,
    MessagePriority,
    MessageType,
    ProcessedMessage,
    RAGQuery,
    ServiceHealth,
    SMSMessage,
)
from backend.shared.streams import SMSEventStream

logger = logging.getLogger("message-router")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SMS_MAX_LENGTH = 160

EMERGENCY_KEYWORDS = [
    "emergency",
    "help",
    "fire",
    "medical",
    "injury",
    "injured",
    "ambulance",
    "security",
    "threat",
    "active shooter",
    "bomb",
    "evacuation",
    "collapse",
]

COMMAND_PREFIX = "/"

TEMPLATE_GREETINGS = [
    "hi",
    "hello",
    "hey",
    "howdy",
    "greetings",
    "good morning",
    "good afternoon",
    "good evening",
    "sup",
    "yo",
]

# ---------------------------------------------------------------------------
# MessageRouter
# ---------------------------------------------------------------------------


class MessageRouter:
    """Core routing logic: classify -> (RAG) -> (LLM) -> respond."""

    def __init__(self) -> None:
        # Service URLs - env vars take precedence, then settings
        self.llm_service_url = os.getenv(
            "LLM_SERVICE_URL", settings.llm_inference_url
        )
        self.rag_service_url = os.getenv(
            "RAG_SERVICE_URL", settings.rag_service_url
        )
        self.privacy_filter_url = os.getenv(
            "PRIVACY_FILTER_URL", settings.privacy_filter_url
        )
        self.sms_gateway_url = os.getenv(
            "SMS_GATEWAY_URL", settings.sms_gateway_url
        )

        # Redis Streams event stream (consumer side)
        self.event_stream = SMSEventStream(
            redis_url=os.getenv("REDIS_URL", settings.redis_url),
            stream_name=settings.stream_name,
            group_name=settings.stream_consumer_group,
        )
        self.consumer_name = os.getenv("NODE_ID", settings.node_id)
        self._stream_task: Optional[asyncio.Task[None]] = None

        # Concurrency control for LLM requests
        self.llm_semaphore = asyncio.Semaphore(settings.llm_max_inflight_requests)

        # Shared HTTP client (created during lifespan startup)
        self.http_client: Optional[httpx.AsyncClient] = None

        # Statistics
        self.stats: Dict[str, int] = {
            "messages_received": 0,
            "messages_classified": 0,
            "messages_routed_rag": 0,
            "messages_routed_llm": 0,
            "messages_responded": 0,
            "messages_failed": 0,
            "emergency_messages": 0,
            "command_messages": 0,
            "template_messages": 0,
            "query_messages": 0,
        }
        self.start_time = time.time()

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify_message(self, message: SMSMessage) -> ProcessedMessage:
        """Determine MessageType, priority, and routing needs."""
        content = message.content.strip()
        content_lower = content.lower()

        # --- Emergency detection ---
        for keyword in EMERGENCY_KEYWORDS:
            if keyword in content_lower:
                self.stats["emergency_messages"] += 1
                return ProcessedMessage(
                    original_message=message,
                    message_type=MessageType.EMERGENCY,
                    intent="emergency",
                    requires_rag=False,
                    requires_llm=False,
                    priority=MessagePriority.EMERGENCY,
                )

        # --- Command detection ---
        if content_lower.startswith(COMMAND_PREFIX):
            self.stats["command_messages"] += 1
            command = content_lower.split()[0] if content_lower.split() else content_lower
            return ProcessedMessage(
                original_message=message,
                message_type=MessageType.COMMAND,
                intent=command,
                requires_rag=False,
                requires_llm=False,
                priority=MessagePriority.NORMAL,
            )

        # --- Template / greeting detection ---
        if content_lower in TEMPLATE_GREETINGS:
            self.stats["template_messages"] += 1
            return ProcessedMessage(
                original_message=message,
                message_type=MessageType.TEMPLATE,
                intent="greeting",
                requires_rag=False,
                requires_llm=False,
                priority=MessagePriority.LOW,
            )

        # --- Default: free-text query -> RAG + LLM ---
        self.stats["query_messages"] += 1
        return ProcessedMessage(
            original_message=message,
            message_type=MessageType.QUERY,
            intent="query",
            requires_rag=True,
            requires_llm=True,
            priority=MessagePriority.NORMAL,
        )

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    async def route_to_rag(self, query: str) -> Optional[str]:
        """Call the RAG service and return retrieved context."""
        if self.http_client is None:
            logger.error("HTTP client not initialised")
            return None

        rag_query = RAGQuery(query=query, top_k=3)
        try:
            response = await self.http_client.post(
                f"{self.rag_service_url}/search",
                json=rag_query.model_dump(),
                timeout=settings.llm_request_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()

            documents = data.get("documents", [])
            if documents:
                self.stats["messages_routed_rag"] += 1
                return "\n\n".join(documents)
            return None
        except Exception as exc:
            logger.warning("RAG service call failed: %s", exc)
            return None

    async def route_to_llm(self, prompt: str, context: Optional[str] = None) -> Optional[str]:
        """Call the LLM inference service with concurrency control."""
        if self.http_client is None:
            logger.error("HTTP client not initialised")
            return None

        llm_request = LLMRequest(
            prompt=prompt,
            context=context,
            max_length=SMS_MAX_LENGTH,
            temperature=0.7,
        )

        async with self.llm_semaphore:
            try:
                response = await self.http_client.post(
                    f"{self.llm_service_url}/inference",
                    json=llm_request.model_dump(),
                    timeout=settings.llm_request_timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                self.stats["messages_routed_llm"] += 1
                llm_response = LLMResponse(**data)
                return llm_response.response
            except Exception as exc:
                logger.warning("LLM service call failed: %s", exc)
                return None

    async def send_response(self, recipient: str, sender: str, text: str) -> bool:
        """Send the response back via SMS gateway, chunking if necessary."""
        if self.http_client is None:
            logger.error("HTTP client not initialised")
            return False

        chunks = _chunk_sms_response(text)
        success = True
        for i, chunk in enumerate(chunks):
            sms = SMSMessage(
                id=str(uuid.uuid4()),
                sender=sender,
                receiver=recipient,
                content=chunk,
                timestamp=datetime.utcnow(),
                priority=MessagePriority.NORMAL,
                metadata={"part": i + 1, "total_parts": len(chunks)},
            )
            try:
                response = await self.http_client.post(
                    f"{self.sms_gateway_url}/sms/send",
                    json=sms.model_dump(mode="json"),
                    timeout=settings.sms_router_timeout_seconds,
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning("SMS send failed (part %d/%d): %s", i + 1, len(chunks), exc)
                success = False
        return success

    # ------------------------------------------------------------------
    # Template / command responses
    # ------------------------------------------------------------------

    def _handle_template(self, processed: ProcessedMessage) -> str:
        """Return a canned greeting response."""
        return (
            "Welcome to Summit Connect! "
            "Ask about sessions, speakers, venues, or local area info."
        )

    def _handle_command(self, processed: ProcessedMessage) -> str:
        """Return a canned command response."""
        command = (processed.intent or "").lower()

        if command == "/help":
            return (
                "Commands: /help, /status, /schedule, /speakers. "
                "Or just text your question!"
            )

        if command == "/status":
            uptime = int(time.time() - self.start_time)
            return (
                f"Summit Connect Assistant status: "
                f"Messages received: {self.stats['messages_received']} | "
                f"Responded: {self.stats['messages_responded']} | "
                f"Failed: {self.stats['messages_failed']} | "
                f"Uptime: {uptime}s"
            )

        if command == "/schedule":
            return (
                "Text your question about the schedule and "
                "I'll find sessions for you!"
            )

        if command == "/speakers":
            return (
                "Text a topic and I'll find relevant speakers and sessions!"
            )

        return (
            "Unknown command. "
            "Commands: /help, /status, /schedule, /speakers. "
            "Or just text your question!"
        )

    def _handle_emergency(self, processed: ProcessedMessage) -> str:
        """Return an emergency response directing to event security."""
        return (
            "EMERGENCY: Your message has been flagged. "
            "Please contact event security immediately or go to the nearest "
            "information desk. If this is a life-threatening emergency, call 911."
        )

    # ------------------------------------------------------------------
    # Redis Streams consumer loop
    # ------------------------------------------------------------------

    async def _stream_consumer_loop(self) -> None:
        """Continuously consume messages from the Redis Stream."""
        logger.info(
            "Stream consumer loop started (consumer=%s, stream=%s)",
            self.consumer_name,
            self.event_stream.stream_name,
        )
        while True:
            try:
                messages = await self.event_stream.consume(
                    consumer_name=self.consumer_name,
                    count=1,
                    block_ms=5000,
                )

                if not messages:
                    await asyncio.sleep(0.1)
                    continue

                for msg_id, fields in messages:
                    try:
                        # Reconstruct an SMSMessage from the stream fields
                        metadata_raw = fields.get("metadata", "{}")
                        try:
                            metadata = json.loads(metadata_raw) if metadata_raw else {}
                        except (json.JSONDecodeError, TypeError):
                            metadata = {}

                        priority_str = fields.get("priority", "normal")
                        try:
                            priority = MessagePriority(priority_str)
                        except ValueError:
                            priority = MessagePriority.NORMAL

                        sms_message = SMSMessage(
                            id=msg_id,
                            sender=fields.get("sender", "unknown"),
                            receiver=fields.get("receiver", "system"),
                            content=fields.get("content", ""),
                            timestamp=fields.get("timestamp", datetime.utcnow().isoformat()),
                            priority=priority,
                            metadata=metadata if metadata else None,
                        )

                        logger.info(
                            "Stream consumer processing message %s from %s",
                            msg_id,
                            sms_message.sender,
                        )
                        await self.process_message(sms_message)

                        # ACK only after successful processing
                        await self.event_stream.ack(msg_id)
                        self.stats["stream_messages_processed"] = (
                            self.stats.get("stream_messages_processed", 0) + 1
                        )

                    except Exception:
                        # Do NOT ack -- message will be redelivered
                        logger.exception(
                            "Failed to process stream message %s -- will be redelivered",
                            msg_id,
                        )
                        self.stats["stream_messages_failed"] = (
                            self.stats.get("stream_messages_failed", 0) + 1
                        )

            except asyncio.CancelledError:
                logger.info("Stream consumer loop cancelled")
                raise
            except Exception:
                logger.exception("Stream consumer loop error -- retrying in 1s")
                await asyncio.sleep(1.0)

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    async def process_message(self, message: SMSMessage) -> str:
        """Full processing pipeline: classify -> route -> respond."""
        self.stats["messages_received"] += 1
        logger.info(
            "Processing message from %s: %.40s...",
            message.sender,
            message.content,
        )

        # 1. Classify
        processed = self.classify_message(message)
        self.stats["messages_classified"] += 1
        logger.info(
            "Classified as %s (priority=%s, rag=%s, llm=%s)",
            processed.message_type.value,
            processed.priority.value,
            processed.requires_rag,
            processed.requires_llm,
        )

        # 2. Handle non-routed message types directly
        if processed.message_type == MessageType.EMERGENCY:
            response_text = self._handle_emergency(processed)
        elif processed.message_type == MessageType.COMMAND:
            response_text = self._handle_command(processed)
        elif processed.message_type == MessageType.TEMPLATE:
            response_text = self._handle_template(processed)
        else:
            # 3. RAG retrieval (if needed)
            context = None
            if processed.requires_rag:
                context = await self.route_to_rag(message.content)

            # 4. LLM inference (if needed)
            if processed.requires_llm:
                llm_result = await self.route_to_llm(message.content, context)
                if llm_result:
                    response_text = llm_result
                else:
                    response_text = (
                        "Sorry, I couldn't process your request right now. "
                        "Please try again shortly."
                    )
            else:
                response_text = (
                    "Sorry, I couldn't process your request right now. "
                    "Please try again shortly."
                )

        # 5. Send response back via SMS gateway
        sent = await self.send_response(
            recipient=message.sender,
            sender=message.receiver,
            text=response_text,
        )
        if sent:
            self.stats["messages_responded"] += 1
        else:
            self.stats["messages_failed"] += 1
            logger.warning("Failed to send response to %s", message.sender)

        return response_text


# ---------------------------------------------------------------------------
# SMS chunking
# ---------------------------------------------------------------------------


def _chunk_sms_response(text: str, max_length: int = SMS_MAX_LENGTH) -> List[str]:
    """Split a long response into SMS-sized chunks.

    If the text fits in a single SMS it is returned as-is.
    Otherwise, each chunk is suffixed with `` (N/M)`` and split on word
    boundaries where possible.
    """
    if len(text) <= max_length:
        return [text]

    # Reserve space for the " (NN/NN)" suffix (up to 8 chars)
    suffix_reserve = 8
    effective_max = max_length - suffix_reserve

    chunks: List[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= effective_max:
            chunks.append(remaining)
            break

        # Try to split on a word boundary
        split_at = remaining.rfind(" ", 0, effective_max)
        if split_at == -1:
            split_at = effective_max

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    # Add part indicators
    total = len(chunks)
    if total > 1:
        chunks = [f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)]

    return chunks


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------

router_instance = MessageRouter()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("Starting Message Router service")
    router_instance.http_client = httpx.AsyncClient()
    logger.info(
        "Service URLs - LLM: %s | RAG: %s | SMS GW: %s | Privacy: %s",
        router_instance.llm_service_url,
        router_instance.rag_service_url,
        router_instance.sms_gateway_url,
        router_instance.privacy_filter_url,
    )

    # Connect to Redis Streams and start the consumer loop
    try:
        await router_instance.event_stream.connect()
        router_instance._stream_task = asyncio.create_task(
            router_instance._stream_consumer_loop()
        )
        logger.info("Redis Streams consumer loop started")
    except Exception:
        logger.warning("Redis Streams unavailable -- HTTP-only intake active")

    yield

    # Shutdown: cancel stream consumer, close connections
    logger.info("Shutting down Message Router service")
    if router_instance._stream_task and not router_instance._stream_task.done():
        router_instance._stream_task.cancel()
        try:
            await router_instance._stream_task
        except asyncio.CancelledError:
            pass
    await router_instance.event_stream.close()
    if router_instance.http_client:
        await router_instance.http_client.aclose()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Edge Inference at Scale - Message Router",
    description="Classifies and routes SMS messages through RAG and LLM services for Summit Connect.",
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health_check() -> ServiceHealth:
    """Basic health check."""
    return ServiceHealth(
        service_name="message-router",
        status="healthy",
        version="1.0.0",
        details={
            "messages_received": router_instance.stats["messages_received"],
            "uptime_seconds": time.time() - router_instance.start_time,
        },
    )


@app.get("/stream/health")
async def stream_health():
    """Return Redis Streams health information."""
    info = await router_instance.event_stream.health()
    return {"stream": info}


@app.post("/route")
async def route_message(message: SMSMessage) -> Dict:
    """Full pipeline: classify, retrieve, infer, respond."""
    try:
        response_text = await router_instance.process_message(message)
        return {
            "status": "success",
            "response": response_text,
            "message_id": message.id,
        }
    except Exception as exc:
        logger.exception("Error processing message: %s", exc)
        router_instance.stats["messages_failed"] += 1
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/classify")
async def classify_message(message: SMSMessage) -> ProcessedMessage:
    """Classify a message without routing it."""
    try:
        processed = router_instance.classify_message(message)
        return processed
    except Exception as exc:
        logger.exception("Error classifying message: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/statistics")
async def get_statistics() -> Dict:
    """Return current message processing statistics."""
    uptime = time.time() - router_instance.start_time
    return {
        "stats": router_instance.stats,
        "uptime_seconds": uptime,
        "service": "message-router",
    }


@app.get("/services/status")
async def get_services_status() -> Dict:
    """Check health of downstream services."""
    services = {
        "llm_inference": router_instance.llm_service_url,
        "rag_service": router_instance.rag_service_url,
        "sms_gateway": router_instance.sms_gateway_url,
        "privacy_filter": router_instance.privacy_filter_url,
    }

    results: Dict[str, Dict] = {}
    client = router_instance.http_client

    for name, url in services.items():
        try:
            if client is None:
                raise RuntimeError("HTTP client not initialised")
            resp = await client.get(f"{url}/health", timeout=5.0)
            resp.raise_for_status()
            results[name] = {"status": "healthy", "url": url}
        except Exception as exc:
            results[name] = {"status": "unhealthy", "url": url, "error": str(exc)}

    return {"services": results}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    )
    uvicorn.run(
        "backend.services.message_router.main:app",
        host="0.0.0.0",
        port=settings.message_router_port,
        reload=True,
    )
