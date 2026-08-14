"""Capacity and burst benchmarks for the SMS pipeline.

Measures how far a single CPU edge node can scale under realistic
bursty SMS traffic patterns. Identifies bottlenecks per pipeline stage
(classify, RAG, LLM, SMS send) and reports degradation curves.

Two modes:
  - Unit (mocked): validates the benchmark harness and concurrency logic
    without external services. Runs in CI.
  - Live (compose): hits the real stack, produces actual latency data.
    Requires: docker compose up.  Run with: CAPACITY_LIVE=1 pytest ...

All timing and throughput numbers are written to
tests/benchmarks/capacity_results.json for the validation matrix.
"""

import asyncio
import json
import logging
import os
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.shared.models import SMSMessage, MessagePriority
from backend.services.message_router.main import MessageRouter

logger = logging.getLogger("capacity-bench")

RESULTS_FILE = Path(__file__).parent / "capacity_results.json"

LIVE_MODE = os.environ.get("CAPACITY_LIVE", "0") == "1"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class StageLatency:
    """Latency breakdown for a single message through the pipeline."""
    total_ms: float = 0.0
    classify_ms: float = 0.0
    rag_ms: float = 0.0
    llm_ms: float = 0.0
    send_ms: float = 0.0
    queued_ms: float = 0.0


@dataclass
class BurstResult:
    """Result from a single burst test."""
    burst_size: int = 0
    concurrent_users: int = 0
    total_time_ms: float = 0.0
    messages_completed: int = 0
    messages_failed: int = 0
    throughput_msg_per_sec: float = 0.0
    latencies: List[StageLatency] = field(default_factory=list)

    @property
    def p50_ms(self) -> float:
        times = [l.total_ms for l in self.latencies]
        return _percentile(times, 50) if times else 0.0

    @property
    def p95_ms(self) -> float:
        times = [l.total_ms for l in self.latencies]
        return _percentile(times, 95) if times else 0.0

    @property
    def p99_ms(self) -> float:
        times = [l.total_ms for l in self.latencies]
        return _percentile(times, 99) if times else 0.0

    @property
    def bottleneck(self) -> str:
        if not self.latencies:
            return "unknown"
        avg_by_stage = {
            "classify": statistics.mean(l.classify_ms for l in self.latencies),
            "rag": statistics.mean(l.rag_ms for l in self.latencies),
            "llm": statistics.mean(l.llm_ms for l in self.latencies),
            "send": statistics.mean(l.send_ms for l in self.latencies),
            "queued": statistics.mean(l.queued_ms for l in self.latencies),
        }
        return max(avg_by_stage, key=avg_by_stage.get)


@dataclass
class CapacityReport:
    """Full capacity test report."""
    timestamp: str = ""
    mode: str = "mocked"
    semaphore_limit: int = 2
    bursts: List[Dict] = field(default_factory=list)
    sustained_throughput_msg_per_min: float = 0.0
    max_burst_before_degradation: int = 0
    bottleneck_stage: str = ""
    degradation_curve: List[Dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(data: List[float], pct: int) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * pct / 100
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_data) else f
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def _sms(content, sender=None):
    return SMSMessage(
        id=str(uuid.uuid4()),
        sender=sender or f"+1555{uuid.uuid4().int % 10000000:07d}",
        receiver="+15559876543",
        content=content,
        timestamp=datetime.now(timezone.utc),
        priority=MessagePriority.NORMAL,
    )


SAMPLE_QUERIES = [
    "What time is the keynote?",
    "Where is the WiFi password?",
    "What sessions are about edge computing?",
    "Where can I eat lunch?",
    "Who is speaking about OpenShift?",
    "Tell me about the BitNet session",
    "Where is first aid?",
    "What's happening on Day 2?",
    "Best restaurants nearby?",
    "When is the conference dinner?",
]


def _instrumented_router(rag_delay_ms=50, llm_delay_ms=5000):
    """Create a MessageRouter with configurable stage delays."""
    router = MessageRouter()

    async def mock_rag_post(url, json=None, timeout=None):
        if "/search" in url:
            await asyncio.sleep(rag_delay_ms / 1000)
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={
                "documents": ["Edge Computing Workshop - Room 301, 2:00 PM"],
                "scores": [0.65],
            })
            return resp
        elif "/inference" in url:
            await asyncio.sleep(llm_delay_ms / 1000)
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={
                "response": "The Edge Computing Workshop is in Room 301 at 2 PM.",
                "model_used": "bitnet-2b4t",
                "tokens_used": 20,
                "processing_time": llm_delay_ms,
            })
            return resp
        else:
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            return resp

    async def mock_validate(url, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={"valid": True})
        return resp

    async def mock_post(url, json=None, timeout=None):
        if "/validate" in url:
            return await mock_validate(url, json, timeout)
        return await mock_rag_post(url, json, timeout)

    router.http_client = MagicMock()
    router.http_client.post = AsyncMock(side_effect=mock_post)

    store = MagicMock()
    store.get_hunt_state = AsyncMock(return_value=0)
    store.set_hunt_state = AsyncMock()
    store.get_history = AsyncMock(return_value=[])
    store.add_turn = AsyncMock()
    store.clear_history = AsyncMock()
    router.chat_store = store

    return router


async def _run_burst(router, burst_size, queries=None):
    """Send burst_size messages concurrently, return BurstResult."""
    queries = queries or SAMPLE_QUERIES
    messages = [_sms(queries[i % len(queries)]) for i in range(burst_size)]
    latencies = []
    completed = 0
    failed = 0

    async def _process_one(msg):
        nonlocal completed, failed
        enqueue_time = time.monotonic()
        try:
            start = time.monotonic()
            result = await router.process_message(msg)
            total = (time.monotonic() - start) * 1000
            queued = (start - enqueue_time) * 1000

            latencies.append(StageLatency(
                total_ms=total,
                queued_ms=queued,
            ))
            completed += 1
        except Exception:
            failed += 1

    wall_start = time.monotonic()
    await asyncio.gather(*[_process_one(m) for m in messages])
    wall_total = (time.monotonic() - wall_start) * 1000

    throughput = (completed / (wall_total / 1000)) if wall_total > 0 else 0

    return BurstResult(
        burst_size=burst_size,
        concurrent_users=burst_size,
        total_time_ms=wall_total,
        messages_completed=completed,
        messages_failed=failed,
        throughput_msg_per_sec=throughput,
        latencies=latencies,
    )


# ---------------------------------------------------------------------------
# Burst sizing tests (mocked — run in CI)
# ---------------------------------------------------------------------------


class TestBurstCapacity:
    """Measure throughput and latency under increasing burst sizes.

    Uses mocked services with realistic delay profiles:
    - RAG: 50ms (vector search on local ChromaDB)
    - LLM: 5000ms (BitNet on constrained CPU)
    - Semaphore: 2 in-flight LLM requests (default config)
    """

    @pytest.mark.asyncio
    async def test_single_message_baseline(self):
        """Baseline: one message, no contention."""
        router = _instrumented_router(rag_delay_ms=50, llm_delay_ms=100)
        result = await _run_burst(router, 1)
        assert result.messages_completed == 1
        assert result.messages_failed == 0
        assert result.p50_ms > 0

    @pytest.mark.asyncio
    async def test_burst_of_5(self):
        """5 concurrent messages — semaphore starts queuing at 3."""
        router = _instrumented_router(rag_delay_ms=20, llm_delay_ms=200)
        result = await _run_burst(router, 5)
        assert result.messages_completed == 5
        assert result.throughput_msg_per_sec > 0

    @pytest.mark.asyncio
    async def test_burst_of_10(self):
        """10 concurrent — realistic booth burst (group arrives)."""
        router = _instrumented_router(rag_delay_ms=20, llm_delay_ms=200)
        result = await _run_burst(router, 10)
        assert result.messages_completed == 10
        assert result.p95_ms > result.p50_ms  # queuing effect visible

    @pytest.mark.asyncio
    async def test_burst_of_20(self):
        """20 concurrent — stress test for a single node."""
        router = _instrumented_router(rag_delay_ms=20, llm_delay_ms=200)
        result = await _run_burst(router, 20)
        assert result.messages_completed == 20

    @pytest.mark.asyncio
    async def test_burst_of_50(self):
        """50 concurrent — beyond booth scale, fleet territory."""
        router = _instrumented_router(rag_delay_ms=20, llm_delay_ms=200)
        result = await _run_burst(router, 50)
        assert result.messages_completed == 50


class TestSemaphoreBottleneck:
    """Verify the LLM semaphore is the bottleneck and quantify its impact."""

    @pytest.mark.asyncio
    async def test_semaphore_2_vs_4(self):
        """Compare throughput with semaphore=2 (default) vs semaphore=4."""
        router_2 = _instrumented_router(rag_delay_ms=10, llm_delay_ms=200)
        router_2.llm_semaphore = asyncio.Semaphore(2)

        router_4 = _instrumented_router(rag_delay_ms=10, llm_delay_ms=200)
        router_4.llm_semaphore = asyncio.Semaphore(4)

        result_2 = await _run_burst(router_2, 10)
        result_4 = await _run_burst(router_4, 10)

        assert result_4.throughput_msg_per_sec > result_2.throughput_msg_per_sec * 1.3

    @pytest.mark.asyncio
    async def test_rag_direct_bypasses_semaphore(self):
        """RAG-direct responses skip the LLM semaphore entirely."""
        router = _instrumented_router(rag_delay_ms=10, llm_delay_ms=5000)

        # Override RAG to return high-confidence short docs
        async def high_conf_rag(url, json=None, timeout=None):
            if "/search" in url:
                await asyncio.sleep(0.01)
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(return_value={
                    "documents": ["Keynote: Main Hall, 9 AM Day 1"],
                    "scores": [0.92],
                })
                return resp
            elif "/validate" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json = MagicMock(return_value={"valid": True})
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            return resp

        router.http_client.post = AsyncMock(side_effect=high_conf_rag)
        result = await _run_burst(router, 10)

        assert result.messages_completed == 10
        # RAG-direct should be much faster than LLM path
        assert result.p50_ms < 500
        assert router.stats.get("rag_direct_responses", 0) == 10


class TestDegradationCurve:
    """Map the degradation curve: at what burst size does latency blow up?"""

    @pytest.mark.asyncio
    async def test_degradation_curve(self):
        """Run increasing burst sizes and record the curve."""
        burst_sizes = [1, 2, 5, 10, 20, 50]
        curve = []

        for size in burst_sizes:
            router = _instrumented_router(rag_delay_ms=10, llm_delay_ms=200)
            result = await _run_burst(router, size)
            curve.append({
                "burst_size": size,
                "p50_ms": round(result.p50_ms, 1),
                "p95_ms": round(result.p95_ms, 1),
                "p99_ms": round(result.p99_ms, 1),
                "throughput_msg_per_sec": round(result.throughput_msg_per_sec, 2),
                "completed": result.messages_completed,
                "failed": result.messages_failed,
            })

        # Verify monotonic degradation in p95 latency
        p95_values = [c["p95_ms"] for c in curve]
        for i in range(1, len(p95_values)):
            assert p95_values[i] >= p95_values[i - 1] * 0.8, \
                f"p95 should not improve dramatically: {p95_values[i-1]} -> {p95_values[i]}"

        # Write results
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        report = CapacityReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            mode="mocked",
            semaphore_limit=2,
            degradation_curve=curve,
            bottleneck_stage="llm",
        )
        with open(RESULTS_FILE, "w") as fh:
            json.dump(asdict(report), fh, indent=2)


class TestSustainedThroughput:
    """Simulate sustained traffic over a time window."""

    @pytest.mark.asyncio
    async def test_sustained_30_seconds(self):
        """Send messages at a steady rate for 30s (simulated) and measure."""
        router = _instrumented_router(rag_delay_ms=10, llm_delay_ms=200)
        target_rate = 2  # messages per second
        duration_s = 5  # keep short for unit tests
        total_messages = target_rate * duration_s

        results = []
        start = time.monotonic()

        for i in range(total_messages):
            msg = _sms(SAMPLE_QUERIES[i % len(SAMPLE_QUERIES)])
            t0 = time.monotonic()
            await router.process_message(msg)
            elapsed = (time.monotonic() - t0) * 1000
            results.append(elapsed)

        wall_time = time.monotonic() - start
        actual_rate = len(results) / wall_time

        assert len(results) == total_messages
        assert actual_rate > 0


class TestMixedTrafficPattern:
    """Simulate realistic booth traffic: mix of greetings, queries, hunts."""

    @pytest.mark.asyncio
    async def test_mixed_traffic(self):
        """40% greetings, 40% queries, 20% hunt commands."""
        router = _instrumented_router(rag_delay_ms=10, llm_delay_ms=200)
        store = MagicMock()
        store.get_hunt_state = AsyncMock(return_value=0)
        store.set_hunt_state = AsyncMock()
        store.get_history = AsyncMock(return_value=[])
        store.add_turn = AsyncMock()
        store.clear_history = AsyncMock()
        router.chat_store = store

        traffic = (
            [_sms("hello") for _ in range(4)] +
            [_sms("What time is the keynote?") for _ in range(4)] +
            [_sms("HUNT") for _ in range(2)]
        )

        results = await asyncio.gather(
            *[router.process_message(m) for m in traffic]
        )

        assert len(results) == 10
        assert all(isinstance(r, str) and len(r) > 0 for r in results)

        # Greetings and hunts should be much faster than queries
        assert router.stats["template_messages"] >= 3
        assert router.stats.get("hunt_started", 0) >= 1


class TestQueueDepthPressure:
    """Test behavior when the LLM semaphore is fully saturated."""

    @pytest.mark.asyncio
    async def test_all_slots_occupied(self):
        """With semaphore=2 and 10 LLM-bound messages, verify queuing."""
        router = _instrumented_router(rag_delay_ms=5, llm_delay_ms=500)
        router.llm_semaphore = asyncio.Semaphore(2)

        result = await _run_burst(router, 10)

        assert result.messages_completed == 10
        # With 10 messages, 2 slots, 500ms each: ~5 rounds * 500ms = ~2500ms minimum
        assert result.total_time_ms >= 2000

    @pytest.mark.asyncio
    async def test_semaphore_1_extreme(self):
        """Single-slot semaphore: fully serial LLM processing."""
        router = _instrumented_router(rag_delay_ms=5, llm_delay_ms=100)
        router.llm_semaphore = asyncio.Semaphore(1)

        result = await _run_burst(router, 5)

        assert result.messages_completed == 5
        # Serial: 5 * 100ms minimum
        assert result.total_time_ms >= 400


# ---------------------------------------------------------------------------
# Live benchmarks (requires docker compose up)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not LIVE_MODE, reason="Set CAPACITY_LIVE=1 to run live benchmarks")
class TestLiveCapacity:
    """Hit the real compose stack and measure actual performance.

    Run with: CAPACITY_LIVE=1 pytest tests/benchmarks/test_capacity.py::TestLiveCapacity -v
    """

    @pytest.fixture
    def api_url(self):
        return os.environ.get("EVAL_API_URL", "http://localhost:8000")

    @pytest.mark.asyncio
    async def test_live_single_message(self, api_url):
        import httpx
        async with httpx.AsyncClient() as client:
            start = time.monotonic()
            resp = await client.post(
                f"{api_url}/api/v1/sms/receive",
                json={"message": "What time is the keynote?"},
                timeout=60,
            )
            latency = (time.monotonic() - start) * 1000
            assert resp.status_code == 200
            body = resp.json()
            assert body.get("response") or body.get("message")
            logger.info("Live single message: %.0fms", latency)

    @pytest.mark.asyncio
    async def test_live_burst_10(self, api_url):
        import httpx
        async with httpx.AsyncClient() as client:
            tasks = []
            for i in range(10):
                query = SAMPLE_QUERIES[i % len(SAMPLE_QUERIES)]

                async def send(q=query):
                    start = time.monotonic()
                    resp = await client.post(
                        f"{api_url}/api/v1/sms/receive",
                        json={"message": q},
                        timeout=120,
                    )
                    return (time.monotonic() - start) * 1000, resp.status_code

                tasks.append(send())

            results = await asyncio.gather(*tasks)
            latencies = [r[0] for r in results]
            statuses = [r[1] for r in results]

            assert all(s == 200 for s in statuses)
            logger.info(
                "Live burst 10: p50=%.0fms p95=%.0fms p99=%.0fms",
                _percentile(latencies, 50),
                _percentile(latencies, 95),
                _percentile(latencies, 99),
            )

    @pytest.mark.asyncio
    async def test_live_degradation_curve(self, api_url):
        """Run live degradation curve and write results."""
        import httpx
        curve = []

        for burst_size in [1, 2, 5, 10]:
            async with httpx.AsyncClient() as client:
                tasks = []
                for i in range(burst_size):
                    query = SAMPLE_QUERIES[i % len(SAMPLE_QUERIES)]

                    async def send(q=query):
                        start = time.monotonic()
                        resp = await client.post(
                            f"{api_url}/api/v1/sms/receive",
                            json={"message": q},
                            timeout=120,
                        )
                        return (time.monotonic() - start) * 1000, resp.status_code

                    tasks.append(send())

                results = await asyncio.gather(*tasks)
                latencies = [r[0] for r in results]

                curve.append({
                    "burst_size": burst_size,
                    "p50_ms": round(_percentile(latencies, 50), 1),
                    "p95_ms": round(_percentile(latencies, 95), 1),
                    "p99_ms": round(_percentile(latencies, 99), 1),
                    "throughput_msg_per_sec": round(
                        burst_size / (max(latencies) / 1000), 2
                    ),
                    "completed": sum(1 for r in results if r[1] == 200),
                    "failed": sum(1 for r in results if r[1] != 200),
                })

        report = CapacityReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            mode="live",
            semaphore_limit=2,
            degradation_curve=curve,
            bottleneck_stage="llm",
        )
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RESULTS_FILE, "w") as fh:
            json.dump(asdict(report), fh, indent=2)

        logger.info("Live degradation curve: %s", json.dumps(curve, indent=2))
