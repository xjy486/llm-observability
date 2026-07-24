#!/usr/bin/env python3
"""
Real End-to-End Test: SDK → Proxy → Core → Real LLM API

This test starts real Core and Proxy services, then uses the SDK
with a real LLM API (Agnes 2.0 Flash) to verify the full telemetry
pipeline produces correct span trees.

Architecture:
    SDK (AGENT + LLM spans)
      → injects traceparent + ownership marker headers
      → sends request to Proxy
    Proxy (GATEWAY span)
      → forwards to upstream LLM API (Agnes 2.0 Flash)
      → captures timing, tokens, payload
      → reports GATEWAY span to Core
    Core (FastAPI + SQLite)
      → stores all spans
      → provides query APIs

Usage:
    export AGNES_API_KEY="sk-xxx"
    python real_e2e_test.py
"""
import sys
import os
import time
import json
import socket
import asyncio
import subprocess
import tempfile
import signal
from pathlib import Path

import urllib.request
import urllib.error

# ─── Path Setup ──────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT / "proxy"))
sys.path.insert(0, str(ROOT / "core"))

# ─── Config ───────────────────────────────────────────────────
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")
AGNES_BASE_URL = "https://apihub.agnes-ai.com"
AGNES_MODEL = "agnes-2.0-flash"

# ─── Helpers ──────────────────────────────────────────────────

def find_free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 15.0, interval: float = 0.3) -> bool:
    """Wait for an HTTP server to become responsive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(interval)
    return False


def http_get_json(url: str, timeout: float = 5.0) -> dict:
    """Simple synchronous GET that returns JSON."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url: str, body: dict, headers: dict = None, timeout: float = 5.0) -> dict:
    """Simple synchronous POST that returns JSON."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─── Service Management ───────────────────────────────────────

class ServiceManager:
    """Manages Core and Proxy subprocesses."""

    def __init__(self):
        self.core_port = find_free_port()
        self.proxy_port = find_free_port()
        self.db_path = tempfile.mktemp(suffix="_e2e.db")
        self.core_proc = None
        self.proxy_proc = None

    @property
    def core_url(self) -> str:
        return f"http://127.0.0.1:{self.core_port}"

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    def start_core(self):
        """Start the Core FastAPI server."""
        env = os.environ.copy()
        env["DB_PATH"] = self.db_path

        env["PYTHONPATH"] = str(ROOT / "core")

        cmd = [
            sys.executable, "-m", "uvicorn",
            "api.main:app",
            "--host", "127.0.0.1",
            "--port", str(self.core_port),
        ]
        self.core_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT / "core"),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not wait_for_server(f"{self.core_url}/api/v1/health"):
            out, err = self.core_proc.communicate(timeout=5)
            raise RuntimeError(
                f"Core failed to start on port {self.core_port}\n"
                f"stdout: {out.decode()[:2000]}\n"
                f"stderr: {err.decode()[:2000]}"
            )
        print(f"  ✅ Core started on port {self.core_port}")

    def start_proxy(self):
        """Start the Proxy server."""
        env = os.environ.copy()
        env["PROXY_HOST"] = "127.0.0.1"
        env["PROXY_PORT"] = str(self.proxy_port)
        env["UPSTREAM_URL"] = AGNES_BASE_URL
        env["OBSERVABILITY_ENDPOINT"] = self.core_url
        env["PAYLOAD_STRATEGY"] = "masked"
        env["GATEWAY_NAME"] = "e2e-test-gateway"

        cmd = [
            sys.executable, "main.py",
        ]
        self.proxy_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT / "proxy"),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not wait_for_server(f"{self.proxy_url}/health"):
            out, err = self.proxy_proc.communicate(timeout=5)
            raise RuntimeError(
                f"Proxy failed to start on port {self.proxy_port}\n"
                f"stdout: {out.decode()[:2000]}\n"
                f"stderr: {err.decode()[:2000]}"
            )
        print(f"  ✅ Proxy started on port {self.proxy_port}")

    def stop_all(self):
        """Stop all services."""
        for proc, name in [
            (self.proxy_proc, "Proxy"),
            (self.core_proc, "Core"),
        ]:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                print(f"  🛑 {name} stopped")

        # Clean up DB
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)


# ─── Test Results Tracker ─────────────────────────────────────

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


# ─── Main Test ────────────────────────────────────────────────

async def run_e2e_tests(svc: ServiceManager):
    """Run all E2E test scenarios."""
    import openai
    from llm_observability import Observability

    # ═════════════════════════════════════════════════════════
    # Initialize SDK ONCE — start the async reporter manually
    # so AGENT/LLM spans get flushed to Core.
    # ═════════════════════════════════════════════════════════
    Observability.init(
        app_name="e2e-real-test",
        endpoint=svc.core_url,
        auto_instrument_openai=True,
    )
    # Reporter needs an event loop to start its background flush task
    await Observability._reporter.start()
    print("  ✅ SDK initialized, reporter started")

    # Create a single OpenAI client pointing to proxy
    client = openai.OpenAI(
        api_key=AGNES_API_KEY,
        base_url=svc.proxy_url + "/v1",
    )

    # ═════════════════════════════════════════════════════════
    # Scenario 1: Basic chat completion (non-streaming)
    # Verifies: AGENT → LLM → GATEWAY span tree
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 1: Basic Chat Completion (non-streaming)")
    print("=" * 70)

    with Observability.trace(
        name="basic-chat-task",
        session_id="e2e-session-1",
        user_id="e2e-user-1",
        business_scene="testing",
    ):
        response = client.chat.completions.create(
            model=AGNES_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Reply concisely."},
                {"role": "user", "content": "What is 2+2? Reply with just the number."},
            ],
            max_tokens=50,
            temperature=0,
        )

    check("Real LLM response received", response is not None)
    check(
        "Response has content",
        bool(response.choices[0].message.content),
        f"content={response.choices[0].message.content!r}",
    )
    check(
        "Response has usage info",
        response.usage is not None and response.usage.total_tokens > 0,
        f"usage={response.usage}",
    )

    print("  ⏳ Waiting for async flush (8s)...")
    await asyncio.sleep(8)

    # ─── Verify Core has data ───
    print("\n  --- Querying Core API ---")

    health = http_get_json(f"{svc.core_url}/api/v1/health")
    check("Core health OK", health.get("status") == "healthy", f"health={health}")
    check("Core has spans", health.get("span_count", 0) >= 2, f"span_count={health.get('span_count')}")

    metrics = http_get_json(f"{svc.core_url}/api/v1/metrics?durationMinutes=5")
    check("Metrics has trace_count", metrics.get("trace_count", 0) >= 1, f"metrics={metrics}")
    check("Metrics has llm_call_count", metrics.get("llm_call_count", 0) >= 1, f"llm_call_count={metrics.get('llm_call_count')}")
    check("Metrics has total_tokens", metrics.get("total_tokens", 0) > 0, f"total_tokens={metrics.get('total_tokens')}")

    traces_resp = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=10")
    traces = traces_resp.get("traces", [])
    check("Trace list non-empty", len(traces) >= 1, f"traces_count={len(traces)}")

    if traces:
        trace = traces[0]
        trace_id = trace["trace_id"]
        check("Trace has app_name", trace.get("app_name") == "e2e-real-test", f"app_name={trace.get('app_name')}")
        check("Trace has session_id", trace.get("session_id") == "e2e-session-1", f"session_id={trace.get('session_id')}")
        check("Trace has user_id", trace.get("user_id") == "e2e-user-1", f"user_id={trace.get('user_id')}")
        check("Trace has business_scene", trace.get("business_scene") == "testing", f"business_scene={trace.get('business_scene')}")
        check("Trace status is OK", trace.get("status") == "OK", f"status={trace.get('status')}")
        check("Trace has span_count >= 2", trace.get("span_count", 0) >= 2, f"span_count={trace.get('span_count')}")
        check("Trace has llm_call_count >= 1", trace.get("llm_call_count", 0) >= 1, f"llm_call_count={trace.get('llm_call_count')}")
        check("Trace has total_tokens > 0", trace.get("total_tokens", 0) > 0, f"total_tokens={trace.get('total_tokens')}")

        detail = http_get_json(f"{svc.core_url}/api/v1/traces/{trace_id}")
        check("Trace detail found", detail is not None, f"trace_id={trace_id}")
        check("Trace detail has spans", len(detail.get("spans", [])) >= 2, f"spans_count={len(detail.get('spans', []))}")

        spans = detail.get("spans", [])
        span_kinds = {s["span_kind"] for s in spans}
        check("Has AGENT span", "AGENT" in span_kinds, f"kinds={span_kinds}")
        check("Has LLM span(s)", "LLM" in span_kinds, f"kinds={span_kinds}")

        agent_spans = [s for s in spans if s["span_kind"] == "AGENT"]
        llm_spans = [s for s in spans if s["span_kind"] == "LLM"]
        gateway_spans = [s for s in spans if s["span_kind"] == "GATEWAY"]

        check("Exactly 1 AGENT span", len(agent_spans) == 1, f"count={len(agent_spans)}")
        check("At least 1 LLM span", len(llm_spans) >= 1, f"count={len(llm_spans)}")

        if agent_spans:
            agent = agent_spans[0]
            check("AGENT parent is None", agent["parent_span_id"] is None, f"parent={agent['parent_span_id']}")

        if agent_spans and llm_spans:
            agent = agent_spans[0]
            llm = llm_spans[0]
            check("LLM has same trace_id as AGENT", llm["trace_id"] == agent["trace_id"], f"llm_trace={llm['trace_id']}, agent_trace={agent['trace_id']}")
            check("LLM parent is AGENT span_id", llm["parent_span_id"] == agent["span_id"], f"llm_parent={llm['parent_span_id']}, agent_id={agent['span_id']}")

        if gateway_spans and llm_spans:
            gw = gateway_spans[0]
            llm = llm_spans[0]
            check("GATEWAY has same trace_id as LLM", gw["trace_id"] == llm["trace_id"], f"gw_trace={gw['trace_id']}, llm_trace={llm['trace_id']}")
            check("GATEWAY parent is LLM span_id", gw["parent_span_id"] == llm["span_id"], f"gw_parent={gw['parent_span_id']}, llm_id={llm['span_id']}")
            check("GATEWAY span_name is proxy.request", gw["span_name"] == "proxy.request", f"name={gw['span_name']}")

        if llm_spans:
            llm = llm_spans[0]
            attrs = llm.get("attributes", {})
            check("LLM has gen_ai.request.model", "gen_ai.request.model" in attrs, f"attrs_keys={list(attrs.keys())}")
            check("LLM model is agnes-2.0-flash", attrs.get("gen_ai.request.model") == AGNES_MODEL, f"model={attrs.get('gen_ai.request.model')}")
            check("LLM has token usage", attrs.get("gen_ai.usage.total_tokens", 0) > 0, f"tokens={attrs.get('gen_ai.usage.total_tokens')}")

        if gateway_spans:
            gw = gateway_spans[0]
            check("GATEWAY has duration_ms > 0", gw.get("duration_ms", 0) > 0, f"duration={gw.get('duration_ms')}")
            check("GATEWAY has http_status 200", gw.get("http_status") == 200, f"http_status={gw.get('http_status')}")
            check("GATEWAY status is OK", gw.get("status") == "OK", f"status={gw.get('status')}")

            gw_attrs = gw.get("attributes", {})
            check("GATEWAY has gen_ai.usage tokens", gw_attrs.get("gen_ai.usage.total_tokens", 0) > 0, f"tokens={gw_attrs.get('gen_ai.usage.total_tokens')}")

            payload = gw.get("payload")
            check("GATEWAY has payload", payload is not None, f"payload_keys={list(payload.keys()) if isinstance(payload, dict) else type(payload)}")
            if payload:
                check("GATEWAY payload has request", bool(payload.get("request")), f"request={type(payload.get('request'))}")
                check("GATEWAY payload has response", bool(payload.get("response")), f"response={type(payload.get('response'))}")

    # ═════════════════════════════════════════════════════════
    # Scenario 2: Streaming chat completion
    # Verifies: streaming timing metrics (first_chunk_ms, ttft_ms)
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 2: Streaming Chat Completion")
    print("=" * 70)

    collected_content = []
    with Observability.trace(
        name="streaming-chat-task",
        session_id="e2e-session-2",
        user_id="e2e-user-2",
    ):
        stream = client.chat.completions.create(
            model=AGNES_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Count from 1 to 5, one number per line."},
            ],
            max_tokens=100,
            temperature=0,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                collected_content.append(chunk.choices[0].delta.content)

    check("Streaming collected content", len(collected_content) > 0, f"chunks={len(collected_content)}")

    print("  ⏳ Waiting for async flush (8s)...")
    await asyncio.sleep(8)

    traces_resp2 = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=10")
    traces2 = traces_resp2.get("traces", [])
    stream_trace = next((t for t in traces2 if t.get("session_id") == "e2e-session-2"), None)
    check("Streaming trace found", stream_trace is not None, f"traces={[t.get('session_id') for t in traces2]}")

    if stream_trace:
        stream_detail = http_get_json(f"{svc.core_url}/api/v1/traces/{stream_trace['trace_id']}")
        stream_spans = stream_detail.get("spans", [])
        gw_spans = [s for s in stream_spans if s["span_kind"] == "GATEWAY"]
        check("Streaming trace has GATEWAY span", len(gw_spans) >= 1, f"kinds={set(s['span_kind'] for s in stream_spans)}")

        if gw_spans:
            gw = gw_spans[0]
            check("GATEWAY is_stream is True", gw.get("is_stream") is True, f"is_stream={gw.get('is_stream')}")
            check("GATEWAY has first_chunk_ms", gw.get("first_chunk_ms") is not None and gw["first_chunk_ms"] > 0, f"first_chunk_ms={gw.get('first_chunk_ms')}")
            check("GATEWAY has ttft_ms", gw.get("ttft_ms") is not None and gw["ttft_ms"] > 0, f"ttft_ms={gw.get('ttft_ms')}")
            check("GATEWAY duration > first_chunk_ms", gw.get("duration_ms", 0) > gw.get("first_chunk_ms", 0), f"duration={gw.get('duration_ms')}, first_chunk={gw.get('first_chunk_ms')}")

            payload = gw.get("payload")
            if payload and payload.get("response"):
                resp = payload["response"]
                if isinstance(resp, dict):
                    choices = resp.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        check("Streaming response aggregated with content", bool(content), f"content_preview={content[:80] if content else 'EMPTY'}")

    # ═════════════════════════════════════════════════════════
    # Scenario 3: Multiple LLM calls in one trace
    # Verifies: same trace_id across multiple LLM calls
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 3: Multiple LLM Calls in One Trace")
    print("=" * 70)

    with Observability.trace(
        name="multi-llm-task",
        session_id="e2e-session-3",
        user_id="e2e-user-3",
    ):
        resp1 = client.chat.completions.create(
            model=AGNES_MODEL,
            messages=[{"role": "user", "content": "What is the capital of France? One word."}],
            max_tokens=20,
            temperature=0,
        )
        resp2 = client.chat.completions.create(
            model=AGNES_MODEL,
            messages=[{"role": "user", "content": "What is the capital of Japan? One word."}],
            max_tokens=20,
            temperature=0,
        )

    check("First LLM call succeeded", resp1 is not None and bool(resp1.choices[0].message.content))
    check("Second LLM call succeeded", resp2 is not None and bool(resp2.choices[0].message.content))

    print("  ⏳ Waiting for async flush (8s)...")
    await asyncio.sleep(8)

    traces_resp3 = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=20")
    traces3 = traces_resp3.get("traces", [])
    multi_trace = next((t for t in traces3 if t.get("session_id") == "e2e-session-3"), None)
    check("Multi-LLM trace found", multi_trace is not None)

    if multi_trace:
        check("Multi-LLM trace has llm_call_count >= 2", multi_trace.get("llm_call_count", 0) >= 2, f"llm_call_count={multi_trace.get('llm_call_count')}")
        check("Multi-LLM trace has span_count >= 4", multi_trace.get("span_count", 0) >= 4, f"span_count={multi_trace.get('span_count')} (AGENT + 2*LLM + 2*GATEWAY = 5)")

        multi_detail = http_get_json(f"{svc.core_url}/api/v1/traces/{multi_trace['trace_id']}")
        multi_spans = multi_detail.get("spans", [])

        trace_ids = {s["trace_id"] for s in multi_spans}
        check("All spans share same trace_id", len(trace_ids) == 1, f"trace_ids={trace_ids}")

        llm_spans_multi = [s for s in multi_spans if s["span_kind"] == "LLM"]
        check("Multi-LLM trace has 2 LLM spans", len(llm_spans_multi) == 2, f"llm_count={len(llm_spans_multi)}")

        span_ids = {s["span_id"] for s in llm_spans_multi}
        check("LLM spans have unique span_ids", len(span_ids) == 2, f"span_ids={span_ids}")

    # ═════════════════════════════════════════════════════════
    # Scenario 4: Error handling
    # Verifies: error spans are captured correctly
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 4: Error Handling (invalid model)")
    print("=" * 70)

    error_raised = False
    try:
        with Observability.trace(
            name="error-task",
            session_id="e2e-session-4",
        ):
            client.chat.completions.create(
                model="nonexistent-model-xyz",
                messages=[{"role": "user", "content": "test"}],
                max_tokens=10,
            )
    except Exception as e:
        error_raised = True
        print(f"  ℹ️  Expected error: {type(e).__name__}: {str(e)[:100]}")

    check("Error was raised (not swallowed)", error_raised)

    print("  ⏳ Waiting for async flush (8s)...")
    await asyncio.sleep(8)

    traces_resp4 = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=20")
    traces4 = traces_resp4.get("traces", [])
    error_trace = next((t for t in traces4 if t.get("session_id") == "e2e-session-4"), None)
    check("Error trace found", error_trace is not None)

    if error_trace:
        check("Error trace status is ERROR", error_trace.get("status") == "ERROR", f"status={error_trace.get('status')}")

        error_detail = http_get_json(f"{svc.core_url}/api/v1/traces/{error_trace['trace_id']}")
        error_spans = error_detail.get("spans", [])
        error_spans_found = [s for s in error_spans if s.get("status") == "ERROR"]
        check("At least one ERROR span exists", len(error_spans_found) >= 1, f"error_spans={len(error_spans_found)}")

    # ═════════════════════════════════════════════════════════
    # Scenario 5: Models list and dashboard metrics
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 5: Models List & Dashboard Metrics")
    print("=" * 70)

    models_resp = http_get_json(f"{svc.core_url}/api/v1/models")
    models = models_resp.get("models", [])
    check("Models list non-empty", len(models) >= 1, f"models_count={len(models)}")

    agnes_model = next((m for m in models if m.get("model") == AGNES_MODEL), None)
    check("Agnes model in models list", agnes_model is not None, f"models={[m.get('model') for m in models]}")

    if agnes_model:
        check("Agnes model has trace_count > 0", agnes_model.get("trace_count", 0) > 0, f"trace_count={agnes_model.get('trace_count')}")
        check("Agnes model has llm_call_count > 0", agnes_model.get("llm_call_count", 0) > 0, f"llm_call_count={agnes_model.get('llm_call_count')}")

    final_metrics = http_get_json(f"{svc.core_url}/api/v1/metrics?durationMinutes=5")
    check("Final metrics trace_count >= 3", final_metrics.get("trace_count", 0) >= 3, f"trace_count={final_metrics.get('trace_count')}")
    check("Final metrics llm_call_count >= 3", final_metrics.get("llm_call_count", 0) >= 3, f"llm_call_count={final_metrics.get('llm_call_count')}")
    check("Final metrics total_tokens > 0", final_metrics.get("total_tokens", 0) > 0, f"total_tokens={final_metrics.get('total_tokens')}")
    check("Final metrics has p50_latency_ms", final_metrics.get("p50_latency_ms", 0) > 0, f"p50={final_metrics.get('p50_latency_ms')}")
    check("Final metrics has p95_latency_ms", final_metrics.get("p95_latency_ms", 0) > 0, f"p95={final_metrics.get('p95_latency_ms')}")

    ts = http_get_json(f"{svc.core_url}/api/v1/timeseries?durationMinutes=5&intervalSeconds=60")
    check("Time series non-empty", len(ts) > 0, f"ts_count={len(ts)}")
    if ts:
        bucket = ts[0]
        check("Time series has trace_count", "trace_count" in bucket, f"keys={list(bucket.keys())}")
        check("Time series has llm_call_count", "llm_call_count" in bucket)
        check("Time series has span_count", "span_count" in bucket)

    # ═════════════════════════════════════════════════════════
    # Shutdown SDK — flush remaining spans
    # ═════════════════════════════════════════════════════════
    print("\n🧹 Shutting down SDK...")
    await Observability._reporter.stop()
    Observability.shutdown()
    print("  ✅ SDK shutdown complete")


def main():
    global PASS, FAIL

    if not AGNES_API_KEY:
        print("❌ AGNES_API_KEY environment variable is not set.")
        print("   Usage: export AGNES_API_KEY='sk-xxx' && python real_e2e_test.py")
        sys.exit(1)

    print("=" * 70)
    print("Real E2E Test: SDK → Proxy → Core → Agnes 2.0 Flash")
    print("=" * 70)
    print(f"  API Key: {AGNES_API_KEY[:10]}...{AGNES_API_KEY[-4:]}")
    print(f"  Model:   {AGNES_MODEL}")
    print(f"  Base URL: {AGNES_BASE_URL}")

    svc = ServiceManager()

    try:
        # Start services
        print("\n📦 Starting services...")
        svc.start_core()
        svc.start_proxy()

        # Run async tests
        asyncio.run(run_e2e_tests(svc))

    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🧹 Cleaning up services...")
        svc.stop_all()

    # Summary
    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
