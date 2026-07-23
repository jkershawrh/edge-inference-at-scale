"""Edge Inference at Scale -- SMS Gateway service.

Forked from the EVY SMS gateway but stripped of all hardware drivers
(GSM, serial, gammu).  Uses either an in-memory SimDriver for local /
demo work or a TwilioDriver stub for future Phase 4 integration.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from backend.shared.config import settings
from backend.shared.models import (
    ErrorResponse,
    SMSMessage,
    ServiceHealth,
)
from backend.shared.streams import SMSEventStream
from backend.services.sms_gateway.sim_driver import SimDriver
from backend.services.sms_gateway.twilio_driver import TwilioDriver

# Optional EVY-heritage helpers -- degrade gracefully if not yet copied.
try:
    from backend.services.sms_gateway.message_parser import MessageParser  # type: ignore[import-untyped]
    _HAS_PARSER = True
except ImportError:
    _HAS_PARSER = False

try:
    from backend.services.sms_gateway.message_queue import SMSMessageQueue  # type: ignore[import-untyped]
    _HAS_QUEUE = True
except ImportError:
    _HAS_QUEUE = False

logger = logging.getLogger("sms-gateway")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SERVICE_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Request / response models local to this service
# ---------------------------------------------------------------------------


class InboundSMS(BaseModel):
    sender: str
    content: str
    receiver: Optional[str] = None


class OutboundSMS(BaseModel):
    phone_number: str
    content: str


class SendResult(BaseModel):
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Rate-limiter (sliding window)
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Simple per-minute / per-hour sliding-window rate limiter."""

    def __init__(self, per_minute: int, per_hour: int) -> None:
        self._per_minute = per_minute
        self._per_hour = per_hour
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        # Purge entries older than one hour
        while self._timestamps and (now - self._timestamps[0]) > 3600:
            self._timestamps.popleft()
        count_last_hour = len(self._timestamps)
        count_last_minute = sum(1 for t in self._timestamps if (now - t) <= 60)
        if count_last_minute >= self._per_minute:
            return False
        if count_last_hour >= self._per_hour:
            return False
        self._timestamps.append(now)
        return True


# ---------------------------------------------------------------------------
# Gateway class
# ---------------------------------------------------------------------------


class SMSGateway:
    """Core gateway logic -- driver selection, queueing, forwarding."""

    def __init__(self) -> None:
        # Driver selection
        if settings.sms_mode == "twilio":
            logger.info("SMS mode: twilio")
            self.active_driver: Any = TwilioDriver(
                account_sid=settings.twilio_account_sid or "",
                auth_token=settings.twilio_auth_token or "",
                phone_number=settings.twilio_phone_number or "",
            )
        else:
            logger.info("SMS mode: sim (in-memory simulator)")
            self.active_driver = SimDriver()

        # Optional message queue (Redis-backed if available)
        self.message_queue: Optional[Any] = None
        if _HAS_QUEUE:
            try:
                self.message_queue = SMSMessageQueue(
                    redis_url=settings.redis_url,
                )
            except Exception:
                logger.warning("Redis message queue unavailable -- falling back to in-memory only")

        # Optional parser
        self.message_parser: Optional[Any] = None
        if _HAS_PARSER:
            self.message_parser = MessageParser()

        # Redis Streams event stream (producer side)
        self.event_stream = SMSEventStream(
            redis_url=os.getenv("REDIS_URL", settings.redis_url),
            stream_name=settings.stream_name,
            group_name=settings.stream_consumer_group,
        )

        # Rate limiter
        self.rate_limiter = _RateLimiter(
            per_minute=settings.max_sms_per_minute,
            per_hour=settings.max_sms_per_hour,
        )

        # Inbound back-pressure queue (in-memory)
        self._inbound_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(
            maxsize=settings.sms_inbound_queue_maxsize,
        )

        # Metrics
        self._start_time = time.monotonic()
        self._total_received: int = 0
        self._total_sent: int = 0
        self._total_forwarded: int = 0
        self._total_forward_failures: int = 0
        self._failed_forwards: List[Dict[str, Any]] = []

        # Background worker handle
        self._forward_task: Optional[asyncio.Task[None]] = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        ok = await self.active_driver.initialize()
        if not ok:
            raise RuntimeError("SMS driver failed to initialise")
        try:
            await self.event_stream.connect()
            logger.info("Redis Streams event stream connected")
        except Exception:
            logger.warning("Redis Streams unavailable -- will use HTTP-only forwarding")
        self._forward_task = asyncio.create_task(self._forward_worker())
        logger.info("SMS Gateway started (driver=%s)", type(self.active_driver).__name__)

    async def stop(self) -> None:
        if self._forward_task and not self._forward_task.done():
            self._forward_task.cancel()
            try:
                await self._forward_task
            except asyncio.CancelledError:
                pass
        await self.event_stream.close()
        await self.active_driver.disconnect()
        logger.info("SMS Gateway stopped")

    # -- inbound -------------------------------------------------------------

    async def receive_message(self, sender: str, content: str, receiver: Optional[str] = None) -> Dict[str, Any]:
        """Accept an inbound SMS, record it, enqueue for forwarding."""
        if not self.rate_limiter.allow():
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

        msg = await self.active_driver.receive_sms(sender, content)
        self._total_received += 1

        # Parse if available
        parsed: Optional[Dict[str, Any]] = None
        if self.message_parser:
            try:
                parsed_msg = self.message_parser.parse_message(content, sender)
                parsed = {
                    "intent": parsed_msg.intent.value,
                    "category": parsed_msg.category.value,
                    "entities": parsed_msg.entities,
                    "confidence": parsed_msg.confidence,
                    "requires_rag": parsed_msg.requires_rag,
                    "requires_llm": parsed_msg.requires_llm,
                    "priority": parsed_msg.priority,
                }
            except Exception:
                logger.exception("Message parsing failed")

        envelope: Dict[str, Any] = {
            "sender": sender,
            "receiver": receiver or settings.twilio_phone_number or "system",
            "content": content,
            "timestamp": msg["timestamp"],
            "parsed": parsed,
        }

        # Enqueue for async forwarding to the message router
        try:
            self._inbound_queue.put_nowait(envelope)
        except asyncio.QueueFull:
            logger.warning("Inbound queue full -- dropping message from %s", sender)
            raise HTTPException(status_code=503, detail="Back-pressure: inbound queue full")

        return envelope

    # -- outbound ------------------------------------------------------------

    async def send_message(self, phone_number: str, content: str) -> bool:
        """Send an outbound SMS via the active driver."""
        ok = await self.active_driver.send_sms(phone_number, content)
        if ok:
            self._total_sent += 1
        return ok

    # -- history -------------------------------------------------------------

    async def get_history(self) -> List[Dict[str, Any]]:
        """Combined inbox + outbox sorted by timestamp descending."""
        inbox = await self.active_driver.get_inbox()
        outbox = await self.active_driver.get_outbox()
        combined = inbox + outbox
        combined.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        return combined

    # -- forwarding worker ---------------------------------------------------

    async def _forward_worker(self) -> None:
        """Drain the inbound queue and POST each message to the router."""
        while True:
            envelope = await self._inbound_queue.get()
            await self._forward_to_router(envelope)
            self._inbound_queue.task_done()

    async def _forward_to_router(self, envelope: Dict[str, Any]) -> None:
        """Forward a message via Redis Streams, falling back to HTTP POST."""
        # --- Try Redis Streams first ---
        try:
            stream_fields = {
                "sender": envelope.get("sender", ""),
                "receiver": envelope.get("receiver", ""),
                "content": envelope.get("content", ""),
                "timestamp": envelope.get("timestamp", datetime.now(timezone.utc).isoformat()),
                "priority": envelope.get("parsed", {}).get("priority", "normal") if envelope.get("parsed") else "normal",
                "metadata": json.dumps(envelope.get("parsed") or {}),
            }
            msg_id = await self.event_stream.publish(stream_fields)
            self._total_forwarded += 1
            logger.info(
                "Published message from %s to stream (id=%s)",
                envelope.get("sender"),
                msg_id,
            )
            return
        except Exception:
            logger.warning(
                "Redis Streams publish failed for message from %s -- falling back to HTTP",
                envelope.get("sender"),
            )

        # --- HTTP POST fallback (original behaviour) ---
        await self._forward_to_router_http(envelope)

    async def _forward_to_router_http(self, envelope: Dict[str, Any]) -> None:
        """HTTP POST fallback: forward a single message with retries and exponential back-off."""
        url = f"{settings.message_router_url}/route"
        retries = settings.sms_forward_max_retries
        backoff = settings.sms_forward_retry_backoff_seconds

        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=settings.sms_router_timeout_seconds) as client:
                    resp = await client.post(url, json=envelope)
                    resp.raise_for_status()
                self._total_forwarded += 1
                logger.info("Forwarded message from %s to router via HTTP (attempt %d)", envelope.get("sender"), attempt)
                return
            except Exception:
                logger.warning(
                    "HTTP forward attempt %d/%d failed for message from %s",
                    attempt, retries, envelope.get("sender"),
                )
                if attempt < retries:
                    await asyncio.sleep(backoff * attempt)

        # All retries exhausted
        self._total_forward_failures += 1
        failure_record = {**envelope, "failed_at": datetime.now(timezone.utc).isoformat()}
        self._failed_forwards.append(failure_record)
        logger.error("Permanently failed to forward message from %s", envelope.get("sender"))


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

gateway = SMSGateway()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await gateway.start()
    yield
    await gateway.stop()


app = FastAPI(
    title="Edge Inference at Scale - SMS Gateway",
    version=SERVICE_VERSION,
    lifespan=lifespan,
)


# -- health -----------------------------------------------------------------

@app.get("/health", response_model=ServiceHealth)
async def health():
    uptime = time.monotonic() - gateway._start_time
    return ServiceHealth(
        service_name="sms-gateway",
        status="healthy",
        version=SERVICE_VERSION,
        details={
            "sms_mode": settings.sms_mode,
            "driver": type(gateway.active_driver).__name__,
            "total_received": gateway._total_received,
            "total_sent": gateway._total_sent,
            "total_forwarded": gateway._total_forwarded,
            "total_forward_failures": gateway._total_forward_failures,
            "inbound_queue_size": gateway._inbound_queue.qsize(),
            "uptime_seconds": round(uptime, 1),
        },
    )


# -- stream health -----------------------------------------------------------

@app.get("/stream/health")
async def stream_health():
    """Return Redis Streams health information."""
    info = await gateway.event_stream.health()
    return {"stream": info}


# -- receive -----------------------------------------------------------------

@app.post("/sms/receive")
async def sms_receive(body: InboundSMS):
    """Accept an inbound SMS (from the web UI simulator or a webhook)."""
    envelope = await gateway.receive_message(
        sender=body.sender,
        content=body.content,
        receiver=body.receiver,
    )
    return {"status": "queued", "message": envelope}


# -- send --------------------------------------------------------------------

@app.post("/sms/send", response_model=SendResult)
async def sms_send(body: OutboundSMS):
    """Send an outbound SMS."""
    ok = await gateway.send_message(body.phone_number, body.content)
    if not ok:
        raise HTTPException(status_code=502, detail="Driver failed to send SMS")
    return SendResult(success=True, message=f"Sent to {body.phone_number}")


# -- history -----------------------------------------------------------------

@app.get("/sms/history")
async def sms_history():
    """Return combined sent and received messages sorted by timestamp."""
    history = await gateway.get_history()
    return {"messages": history, "count": len(history)}


# -- Twilio webhooks --------------------------------------------------------

@app.post("/twilio/webhook")
async def twilio_webhook(
    request: Request,
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form(""),
):
    """Accept an inbound SMS from Twilio's webhook.

    Validates the X-Twilio-Signature header to prevent forged requests.
    Twilio POSTs form-encoded data with fields ``From``, ``To``, ``Body``,
    ``MessageSid``, etc.
    """
    # Validate Twilio signature to prevent spoofed webhooks
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", settings.twilio_auth_token)
    if auth_token:
        try:
            from twilio.request_validator import RequestValidator
            validator = RequestValidator(auth_token)
            signature = request.headers.get("X-Twilio-Signature", "")
            form_data = dict(await request.form())
            url = str(request.url).replace("http://", "https://")
            if not validator.validate(url, form_data, signature):
                logger.warning("Twilio signature validation FAILED — rejecting request")
                raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        except ImportError:
            logger.warning("twilio package not installed — skipping signature validation")
    logger.info(
        "Twilio webhook: from=%s to=%s sid=%s body=%s",
        From, To, MessageSid, Body[:40],
    )

    envelope = await gateway.receive_message(
        sender=From,
        content=Body,
        receiver=To,
    )

    # Return a TwiML response so Twilio knows we handled the message
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Message>Processing your request...</Message></Response>"
    )
    return Response(content=twiml, media_type="text/xml")


@app.post("/twilio/status")
async def twilio_status(request: Request):
    """Accept delivery status callbacks from Twilio.

    These are informational — we log them and return 200.
    """
    form = await request.form()
    logger.info(
        "Twilio status callback: sid=%s status=%s",
        form.get("MessageSid", ""),
        form.get("MessageStatus", ""),
    )
    return Response(status_code=200)


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
        "backend.services.sms_gateway.main:app",
        host="0.0.0.0",
        port=settings.sms_gateway_port,
        log_level="info",
    )
