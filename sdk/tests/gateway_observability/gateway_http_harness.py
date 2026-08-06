"""Minimal real-HTTP gateway harness + mock Core for E2E (P1-3).

Real chain exercised:

    HTTP client (httpx)
      → GatewayHarness (aiohttp, POST /v1/chat/completions [+ stream])
        → GatewayAdapter → GatewayRuntime → RouterSpan → AttemptSpan
          → mock upstream (deterministic: success / 5xx / timeout / stream)
        → finalize_attempt / finalize_streaming_attempt
      → SDK Reporter (real HTTP POST /api/v1/ingest, background thread)
        → MockCoreServer (real HTTP, stores records)

Deterministic (mock upstream); no network egress, no secrets. Proves the full
glue layer (HTTP → middleware → adapter → runtime → upstream → reporter HTTP →
core ingest API), not just ``runtime.handle_request()`` in memory.
"""
import asyncio
import json
import threading
from typing import Optional

from aiohttp import web

from llm_observability.config import Config
from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    CostCalculator,
)
from llm_observability.gateway_observability.errors import ErrorCategory, GatewayError
from llm_observability.gateway_observability.propagation import inject_downstream_trace_headers
from llm_observability.reporter import Reporter
from llm_observability.tracer import Tracer


# ── Mock Core HTTP server (real /api/v1/ingest) ──────────────────────────

class MockCoreServer:
    """Real aiohttp server accepting POST /api/v1/ingest and storing records.

    The SDK Reporter POSTs real HTTP to it (no ``reporter.report`` monkeypatch).
    Runs on an ephemeral port in a background thread with its own event loop.
    """

    def __init__(self):
        self.records = []
        self._lock = threading.Lock()
        self._runner = None
        self._thread = None
        self._loop = None
        self._port = None
        self._ready = threading.Event()

    async def _ingest(self, request):
        try:
            data = await request.json()
            recs = data.get("records", []) if isinstance(data, dict) else []
        except Exception:
            recs = []
        with self._lock:
            self.records.extend(recs)
        return web.json_response({"status": "ok", "inserted": len(recs)})

    def start(self) -> str:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("MockCoreServer failed to start")
        return self.url

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_post("/api/v1/ingest", self._ingest)
        runner = web.AppRunner(app)
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        self._loop.run_until_complete(site.start())
        self._port = site._server.sockets[0].getsockname()[1]
        self._runner = runner
        self._ready.set()
        self._loop.run_forever()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def snapshot(self):
        with self._lock:
            return list(self.records)

    def stop(self):
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)


# ── Tracer with a real, fast-flushing HTTP Reporter ──────────────────────

def make_tracer(core_url: str) -> Tracer:
    """Build a Tracer whose Reporter POSTs real HTTP to the mock Core.

    Fast flush (0.1s) + batch_size 1 so records reach Core quickly in tests.
    """
    config = Config(
        app_name="gateway-http-e2e", endpoint=core_url,
        auto_instrument_openai=False, auto_instrument_langchain=False,
    )
    reporter = Reporter(endpoint=core_url, flush_interval=0.1, batch_size=1)
    tracer = Tracer(config=config, reporter=reporter)
    reporter.start_sync()
    return tracer


def stop_tracer(tracer: Tracer):
    """Drain + stop the reporter (flushes queued records to Core over HTTP)."""
    try:
        tracer.reporter.stop_sync()
    except Exception:
        pass


# ── Mock upstream (deterministic) ────────────────────────────────────────

def _usage(prompt=10, completion=5):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def mock_response(scenario: str, model: str):
    """Return (response_dict, http_status) for a non-streaming attempt."""
    if scenario == "retry_5xx":
        return {"error": "upstream 5xx", "usage": _usage(10, 0)}, 500
    if scenario == "fallback_timeout":
        # The harness raises a timeout instead of returning; this is unused
        # for the response path but kept for completeness.
        return {"error": "timeout"}, 504
    return {
        "id": "chatcmpl-mock",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": _usage(10, 5),
    }, 200


# Server-side synchronisation event for the deterministic streaming-cancel
# scenario. The test sets it after reading the first chunk; the upstream
# generator blocks on it (no sleep) so the client can disconnect mid-stream
# deterministically.
streaming_cancel_gate = threading.Event()
# Set by the harness when the wrapper's cancel finalize fired.
streaming_cancel_fired = threading.Event()


def mock_stream(scenario: str, model: str):
    """Yield SSE-style content chunks (deterministic).

    ``streaming_cancel`` yields one chunk then blocks on
    ``streaming_cancel_gate`` (set by the test after reading the first chunk)
    so the client can disconnect mid-stream deterministically; the generator
    never yields the rest, forcing the wrapper onto its cancel finalize path.
    """
    if scenario == "streaming_cancel":
        yield {"choices": [{"delta": {"content": "partial"}}]}
        # Block until the test signals (client about to disconnect). Timeout
        # guards against a hang if the test never sets the gate.
        streaming_cancel_gate.wait(timeout=5.0)
        # If we get here the client stayed — emit the rest cleanly.
        yield {"choices": [{"delta": {"content": "rest"}}]}
        yield {"choices": [], "usage": _usage(10, 5)}
        return
    yield {"choices": [{"delta": {"content": "Hello"}}]}
    yield {"choices": [{"delta": {"content": " world"}}]}
    yield {"choices": [], "usage": _usage(10, 5)}


# ── Gateway HTTP harness (aiohttp) ───────────────────────────────────────

class GatewayHarness:
    """Minimal aiohttp gateway wrapping the real GatewayRuntime.

    Exposes POST /v1/chat/completions (and ?stream=1). The scenario is selected
    by the ``X-E2E-Scenario`` header so one endpoint covers all E2E cases.
    """

    def __init__(self, tracer: Tracer, core_url: str):
        self._tracer = tracer
        self._core_url = core_url
        self._runtime = GatewayRuntime(
            tracer=tracer, sample_rate=1.0,
            privacy=PrivacyGuard(secret="harness-secret"),
            cost_calculator=CostCalculator(pricing_table={
                "mock-model": {"input_usd_per_1m_tokens": 1.0, "output_usd_per_1m_tokens": 2.0},
            }),
        )
        self._runner = None
        self._thread = None
        self._loop = None
        self._port = None
        self._ready = threading.Event()

    def start(self) -> str:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("GatewayHarness failed to start")
        return self.url

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        runner = web.AppRunner(app)
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        self._loop.run_until_complete(site.start())
        self._port = site._server.sockets[0].getsockname()[1]
        self._runner = runner
        self._ready.set()
        self._loop.run_forever()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def runtime(self) -> GatewayRuntime:
        return self._runtime

    def stop(self):
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    # ── handler ──

    async def _chat(self, request: web.Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = body.get("model", "mock-model")
        scenario = request.headers.get("X-E2E-Scenario", "success")
        upstream_traceparent = request.headers.get("traceparent")
        stream = bool(body.get("stream")) or request.query.get("stream") == "1"

        request_ctx = {
            "gateway_name": "http-harness",
            "requested_model": model,
            "route": "/v1/chat/completions",
            "provider": "mock",
            "channel_id": "ch-mock",
            "resolved_model": model,
            "channel_type": "openai-compatible",
        }
        handle = self._runtime.handle_request(request_ctx, upstream_traceparent=upstream_traceparent)

        if stream:
            return await self._serve_stream(request, handle, model, scenario)

        return await self._handle_non_stream(handle, model, scenario)

    async def _handle_non_stream(self, handle, model, scenario):
        attempt = handle.start_attempt({"channel_id": "ch-mock", "resolved_model": model, "provider": "mock"})
        attempt.start()

        if scenario == "retry_5xx":
            # Attempt 1 fails 5xx → retry → Attempt 2 succeeds.
            resp1, status1 = mock_response("retry_5xx", model)
            handle.finish_attempt(attempt, response=resp1, upstream_status=status1, raw_usage=resp1.get("usage"))
            attempt.close()
            handle.retry_scheduled(attempt_index=attempt.attempt_index, delay_ms=0, reason="provider_5xx")
            a2 = handle.start_attempt({"channel_id": "ch-mock", "resolved_model": model, "provider": "mock"})
            a2.start()
            resp2, status2 = mock_response("success", model)
            handle.finish_attempt(a2, response=resp2, upstream_status=status2, raw_usage=resp2.get("usage"))
            a2.close()
            handle.finalize()
            return web.json_response(resp2, status=status2)

        if scenario == "fallback_timeout":
            # Attempt 1 times out → fallback → Attempt 2 succeeds.
            handle.finish_attempt(attempt, error=TimeoutError("upstream timeout"))
            attempt.close()
            handle.fallback_selected(from_channel_id="ch-mock", to_channel_id="ch-backup", reason="timeout")
            a2 = handle.start_attempt({"channel_id": "ch-backup", "resolved_model": model, "provider": "mock"})
            a2.start()
            resp2, status2 = mock_response("success", model)
            handle.finish_attempt(a2, response=resp2, upstream_status=status2, raw_usage=resp2.get("usage"))
            a2.close()
            handle.finalize()
            return web.json_response(resp2, status=status2)

        resp, status = mock_response(scenario, model)
        handle.finish_attempt(attempt, response=resp, upstream_status=status, raw_usage=resp.get("usage"))
        attempt.close()
        handle.finalize()
        return web.json_response(resp, status=status)

    async def _serve_stream(self, request, handle, model, scenario):
        attempt = handle.start_attempt({"channel_id": "ch-mock", "resolved_model": model, "provider": "mock"})
        attempt.start()
        chunks = mock_stream(scenario, model)
        wrapped = handle.finalize_streaming_attempt(chunks, attempt)

        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        try:
            for chunk in wrapped:
                # Each chunk is an OpenAI-style dict; emit one SSE data frame.
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            await response.write(b"data: [DONE]\n\n")
        except Exception:
            # Client disconnect mid-stream: finalize the wrapper as cancelled
            # (client_cancelled on Attempt + Router), then stop writing.
            try:
                wrapped.close()
                streaming_cancel_fired.set()
            except Exception:
                pass
        try:
            await response.write_eof()
        except Exception:
            pass
        return response
