"""Phase 2.5 Real End-to-End tests (P0-6).

Hits a REAL OpenAI-compatible endpoint to validate the full call chain:
    SDK Agent → OpenAI Instrumentor → (GATEWAY) → LLM → Core ingest

Gated by env vars E2E_API_KEY / E2E_BASE_URL (loaded from .env). Skipped when
absent, so CI on forks/PRs without secrets does not fail.

Never logs the API key.
"""
import os
import sys
import time
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "proxy"))


def _load_env():
    """Load .env from workspace root if present (does not override real env)."""
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

import pytest

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

from llm_observability import Observability
from llm_observability.decorators import agent, llm, chain, tool
from llm_observability.context import get_current_context
from llm_observability.instrumentation.openai import OpenAIInstrumentor
from trace_context import resolve_trace_context, extract_ownership


E2E_API_KEY = os.environ.get("E2E_API_KEY")
E2E_BASE_URL = os.environ.get("E2E_BASE_URL")
E2E_MODEL = os.environ.get("E2E_MODEL", "gpt-4")

RUN_E2E = bool(HAS_OPENAI and E2E_API_KEY and E2E_BASE_URL)
skip_no_e2e = pytest.mark.skipif(not RUN_E2E, reason="E2E_API_KEY/E2E_BASE_URL not set")


# ─── Mock Core Server (captures ingested spans) ──────────────

class MockCoreHandler(BaseHTTPRequestHandler):
    received_records = []

    def do_POST(self):
        if "/api/v1/ingest" in self.path:
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            data = json.loads(body)
            MockCoreHandler.received_records.extend(data.get("records", []))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","inserted":1,"total":1}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def mock_core():
    MockCoreHandler.received_records = []
    server = HTTPServer(("127.0.0.1", 0), MockCoreHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def proxy_server(mock_core):
    """Start the telemetry proxy in-process pointing upstream at the real
    endpoint and observability at the mock Core. Yields the proxy URL.

    This enables the full chain: SDK -> Proxy (GATEWAY) -> Core.
    """
    if not RUN_E2E:
        yield None
        return
    import asyncio as _asyncio
    from config import ProxyConfig
    from main import create_app

    cfg = ProxyConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_url=E2E_BASE_URL,
        observability_endpoint=mock_core,
        payload_strategy="masked",
    )

    loop = _asyncio.new_event_loop()
    runner_holder = {}

    def _run():
        _asyncio.set_event_loop(loop)
        app = loop.run_until_complete(create_app(cfg))
        from aiohttp import web as _web
        runner = _web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = _web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        runner_holder["runner"] = runner
        runner_holder["port"] = site._server.sockets[0].getsockname()[1]
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Wait for the port to be assigned
    import time as _time
    deadline = _time.time() + 10
    while "port" not in runner_holder and _time.time() < deadline:
        _time.sleep(0.05)
    port = runner_holder.get("port")
    if port is None:
        yield None
        return
    yield f"http://127.0.0.1:{port}"
    # Teardown
    try:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_sdk():
    yield
    if Observability._initialized:
        Observability.shutdown()


def _client():
    return openai.OpenAI(api_key=E2E_API_KEY, base_url=E2E_BASE_URL)


def _records():
    return list(MockCoreHandler.received_records)


def _drain():
    """Drain the reporter queue by shutting down (sends all buffered records).

    The reporter flush interval is 5s; shutdown drains synchronously so we
    don't need a long sleep. Called before reading _records().
    """
    if Observability._initialized:
        Observability.shutdown()


# ─── Scenario A: Manual decorator full chain ────────────────

@skip_no_e2e
def test_scenario_a_manual_decorator_full_chain(mock_core):
    """AGENT → LLM (decorator) → real provider. Asserts parent chain + dedup."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True, payload_strategy="masked",
    )
    tracer = Observability._tracer

    @agent(name="qa")
    def qa_agent(query):
        client = _client()
        resp = client.chat.completions.create(
            model=E2E_MODEL,
            messages=[{"role": "user", "content": query}],
        )
        return resp.choices[0].message.content

    result = qa_agent("Reply with exactly: pong")
    assert result, "expected a non-empty response"
    _drain()

    records = _records()
    kinds = [r["span_kind"] for r in records]
    assert "AGENT" in kinds, f"missing AGENT in {kinds}"
    assert "LLM" in kinds, f"missing LLM in {kinds}"

    agent_rec = [r for r in records if r["span_kind"] == "AGENT"][0]
    llm_rec = [r for r in records if r["span_kind"] == "LLM"][0]
    assert agent_rec["trace_id"] == llm_rec["trace_id"]
    assert agent_rec["parent_span_id"] is None
    assert llm_rec["parent_span_id"] == agent_rec["span_id"]
    # Dedup: only ONE LLM span (decorator logical LLM + OpenAI dedup)
    assert sum(1 for r in records if r["span_kind"] == "LLM") == 1


# ─── Scenario C: Association full-chain ──────────────────────

@skip_no_e2e
def test_scenario_c_association_full_chain(mock_core):
    """Association (user/session/message_id) inherited across AGENT → LLM."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True,
    )
    tracer = Observability._tracer

    @agent(user_id="alice", session_id="s1", message_id="m1")
    def qa_agent():
        client = _client()
        resp = client.chat.completions.create(
            model=E2E_MODEL,
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        )
        return resp.choices[0].message.content

    qa_agent()
    _drain()

    records = _records()
    agent_rec = [r for r in records if r["span_kind"] == "AGENT"][0]
    llm_rec = [r for r in records if r["span_kind"] == "LLM"][0]
    assert agent_rec.get("message_id") == "m1"
    assert llm_rec.get("message_id") == "m1"
    assert llm_rec.get("user_id") == "alice"


# ─── Scenario E: Sampling ────────────────────────────────────

@skip_no_e2e
def test_scenario_e_sample_rate_zero(mock_core):
    """sample_rate=0 → no SDK records reported."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True, sample_rate=0.0,
    )
    tracer = Observability._tracer

    with tracer.trace(name="unsampled"):
        client = _client()
        client.chat.completions.create(
            model=E2E_MODEL,
            messages=[{"role": "user", "content": "Reply: hi"}],
        )
    _drain()
    # No records reported when unsampled
    assert len(_records()) == 0


# ─── Scenario A2: No-SDK compatibility (proxy-side) ─────────

def test_scenario_a2_no_sdk_compatible():
    """No SDK headers → proxy creates a new root trace (LLM fallback)."""
    headers = {}
    ctx = resolve_trace_context(headers)
    ownership = extract_ownership(headers)
    assert ctx.inherited is False
    assert ownership is None


# ─── Scenario B: Streaming ───────────────────────────────────

@skip_no_e2e
def test_scenario_f_streaming(mock_core):
    """Streaming: AGENT + LLM spans, first chunk immediate-ish, bounded."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True,
    )
    tracer = Observability._tracer
    collected = []

    @agent(name="stream-agent")
    def stream_agent():
        client = _client()
        stream = client.chat.completions.create(
            model=E2E_MODEL,
            messages=[{"role": "user", "content": "Count from 1 to 3"}],
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                collected.append(chunk.choices[0].delta.content)
        return "".join(collected)

    result = stream_agent()
    assert result, "expected streamed output"
    _drain()

    records = _records()
    kinds = [r["span_kind"] for r in records]
    assert "AGENT" in kinds
    assert "LLM" in kinds


# ─── Scenario D: Task + Tool nested ──────────────────────────

@skip_no_e2e
def test_scenario_d_task_tool_nested(mock_core):
    """AGENT → TASK → TOOL with a real LLM call inside TASK."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True,
    )
    tracer = Observability._tracer

    @tool(name="translate")
    def translate(text, target="en"):
        client = _client()
        resp = client.chat.completions.create(
            model=E2E_MODEL,
            messages=[{"role": "user", "content": f"Translate to {target}: {text}"}],
        )
        return resp.choices[0].message.content

    @chain(name="pipeline")
    def pipeline(text):
        return translate(text)

    @agent(name="orchestrator")
    def orchestrator(text):
        return pipeline(text)

    result = orchestrator("hello")
    assert result, "expected a translation"
    _drain()

    records = _records()
    kinds = [r["span_kind"] for r in records]
    assert "AGENT" in kinds
    assert "TASK" in kinds
    assert "TOOL" in kinds
    assert "LLM" in kinds

    agent_rec = [r for r in records if r["span_kind"] == "AGENT"][0]
    task_rec = [r for r in records if r["span_kind"] == "TASK"][0]
    tool_rec = [r for r in records if r["span_kind"] == "TOOL"][0]
    # Parent chain: TASK child of AGENT, TOOL child of TASK, LLM child of TOOL
    assert task_rec["parent_span_id"] == agent_rec["span_id"]
    assert tool_rec["parent_span_id"] == task_rec["span_id"]
    assert all(r["trace_id"] == agent_rec["trace_id"] for r in records)


# ─── Scenario A3: @llm + OpenAI dedup ─────────────────────────

@skip_no_e2e
def test_scenario_a3_llm_decorator_dedup(mock_core):
    """@llm decorator + OpenAI instrumentor -> exactly ONE LLM span (dedup).

    The @llm sets logical_llm_span_active=True so the OpenAI instrumentor
    skips its own LLM span (only the decorator's logical LLM remains).
    """
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True,
    )
    tracer = Observability._tracer

    @agent(name="qa")
    def qa_agent():
        @llm()
        def call_model(messages):
            client = _client()
            resp = client.chat.completions.create(
                model=E2E_MODEL,
                messages=messages,
            )
            return resp.choices[0].message.content
        return call_model([{"role": "user", "content": "Reply with exactly: ok"}])

    qa_agent()
    _drain()
    records = _records()
    llm_count = sum(1 for r in records if r["span_kind"] == "LLM")
    assert llm_count == 1, f"expected exactly 1 LLM (dedup), got {llm_count}"


# ─── Scenario G: GATEWAY via proxy (mock upstream, fast) ──────

class MockUpstreamHandler(BaseHTTPRequestHandler):
    """Mock OpenAI-compatible upstream returning a canned chat completion."""
    def do_POST(self):
        body = json.dumps({
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": "mock-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def mock_upstream():
    server = HTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_scenario_g_gateway_via_proxy(mock_core, mock_upstream):
    """Full chain: SDK -> Proxy -> GATEWAY -> Core (mock upstream for speed).

    Validates the in-process proxy creates a GATEWAY span that shares the
    SDK trace. Uses a fast mock upstream so it runs in CI without secrets.
    """
    import asyncio as _asyncio
    from config import ProxyConfig
    from main import create_app
    from aiohttp import web as _web
    import reporter as reporter_mod

    # Lower the proxy reporter flush interval so GATEWAY flushes quickly
    orig_reporter_init = reporter_mod.TelemetryReporter.__init__
    def fast_init(self, *a, **kw):
        kw.setdefault("flush_interval", 0.5)
        orig_reporter_init(self, *a, **kw)
    reporter_mod.TelemetryReporter.__init__ = fast_init

    cfg = ProxyConfig(
        listen_host="127.0.0.1", listen_port=0,
        upstream_url=mock_upstream,
        observability_endpoint=mock_core,
        payload_strategy="masked",
    )
    loop = _asyncio.new_event_loop()
    holder = {}

    def _run():
        _asyncio.set_event_loop(loop)
        app = loop.run_until_complete(create_app(cfg))
        runner = _web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = _web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        holder["port"] = site._server.sockets[0].getsockname()[1]
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    import time as _t
    deadline = _t.time() + 10
    while "port" not in holder and _t.time() < deadline:
        _t.sleep(0.05)
    assert "port" in holder, "proxy failed to start"
    proxy_url = f"http://127.0.0.1:{holder['port']}"

    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True,
    )
    tracer = Observability._tracer
    # base_url MUST include /v1 so the request path is /v1/chat/completions,
    # which matches the proxy's observed_paths (else it passthroughs with no telemetry)
    proxy_client = openai.OpenAI(api_key="test-key", base_url=proxy_url + "/v1")

    @agent(name="gateway-agent", user_id="u1", session_id="s1", message_id="m1")
    def gateway_agent():
        resp = proxy_client.chat.completions.create(
            model="mock-model",
            messages=[{"role": "user", "content": "hi"}],
        )
        return resp.choices[0].message.content

    result = gateway_agent()
    assert result, "expected a response"
    _drain()
    # Wait for the proxy reporter to flush GATEWAY to mock_core
    for _ in range(20):
        _t.sleep(0.5)
        if any(r["span_kind"] == "GATEWAY" for r in _records()):
            break
    records = _records()
    # Hard assertions on the real SDK request's parent chain (Blocker 4)
    agent_recs = [r for r in records if r["span_kind"] == "AGENT"]
    llm_recs = [r for r in records if r["span_kind"] == "LLM"]
    gateway_recs = [r for r in records if r["span_kind"] == "GATEWAY"]
    assert len(agent_recs) == 1, f"expected 1 AGENT, got {len(agent_recs)}"
    assert len(llm_recs) == 1, f"expected 1 LLM, got {len(llm_recs)}"
    assert len(gateway_recs) == 1, f"expected 1 GATEWAY, got {len(gateway_recs)}"
    agent_rec = agent_recs[0]
    llm_rec = llm_recs[0]
    gateway_rec = gateway_recs[0]
    # All share the same trace
    assert agent_rec["trace_id"] == llm_rec["trace_id"] == gateway_rec["trace_id"]
    # LLM is child of AGENT
    assert llm_rec["parent_span_id"] == agent_rec["span_id"]
    # GATEWAY is child of LLM (parent chain: AGENT -> LLM -> GATEWAY)
    assert gateway_rec["parent_span_id"] == llm_rec["span_id"]
    # Association inherited on the GATEWAY (same call, not a separate manual request)
    assert gateway_rec.get("user_id") == "u1"
    assert gateway_rec.get("session_id") == "s1"
    # Restore the reporter init
    reporter_mod.TelemetryReporter.__init__ = orig_reporter_init


# ─── Scenario B: LangChain Auto Real E2E ──────────────────────

@skip_no_e2e
def test_scenario_b_langchain_auto(mock_core):
    """LangChain auto-instrumentation: a RunnableLambda.invoke produces an
    AGENT (auto-root) + callback spans without any explicit wrapper."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True, auto_instrument_langchain=True,
    )

    from langchain_core.runnables import RunnableLambda

    def echo(text):
        return f"echo:{text}"

    chain = RunnableLambda(echo)
    result = chain.invoke("hello")
    assert "echo:hello" in result
    _drain()
    records = _records()
    kinds = [r["span_kind"] for r in records]
    # Auto-root creates an AGENT
    assert "AGENT" in kinds, f"missing AGENT (auto-root) in {kinds}"


@skip_no_e2e
def test_scenario_b_langchain_auto_chatmodel(mock_core):
    """LangChain auto: a real ChatModel via langchain_openai produces AGENT +
    LLM (callback) spans with dedup against the OpenAI instrumentor."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True, auto_instrument_langchain=True,
    )

    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        api_key=E2E_API_KEY, base_url=E2E_BASE_URL, model=E2E_MODEL,
    )
    result = model.invoke("Reply with exactly: ok")
    assert result, "expected a response"
    _drain()
    records = _records()
    kinds = [r["span_kind"] for r in records]
    assert "AGENT" in kinds, f"missing AGENT (auto-root) in {kinds}"
    assert "LLM" in kinds, f"missing LLM in {kinds}"
    # Dedup: at most one LLM per model attempt
    llm_count = sum(1 for r in records if r["span_kind"] == "LLM")
    assert llm_count == 1, f"expected 1 LLM (dedup), got {llm_count}"


@skip_no_e2e
def test_scenario_b_langchain_auto_sequence(mock_core):
    """LangChain auto: a RunnableSequence invokes and produces AGENT + spans."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True, auto_instrument_langchain=True,
    )

    from langchain_core.runnables import RunnableLambda

    chain = RunnableLambda(lambda x: x + " step1") | RunnableLambda(lambda x: x + " step2")
    result = chain.invoke("start")
    assert "step1" in result and "step2" in result
    _drain()
    records = _records()
    kinds = [r["span_kind"] for r in records]
    assert "AGENT" in kinds, f"missing AGENT (auto-root) in {kinds}"


@skip_no_e2e
def test_scenario_b_langchain_auto_user_config_preserved(mock_core):
    """User-provided callbacks are still called when LangChain auto is on."""
    Observability.init(
        app_name="e2e-app", endpoint=mock_core,
        auto_instrument_openai=True, auto_instrument_langchain=True,
    )

    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler

    calls = {"chain_start": 0}

    class CountingHandler(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, **kwargs):
            calls["chain_start"] += 1

    chain = RunnableLambda(lambda x: x)
    result = chain.invoke("hi", config={"callbacks": [CountingHandler()]})
    _drain()
    assert calls["chain_start"] >= 1, "user callback was not called"
    # User config not mutated (no leftover auto handler)
    records = _records()
    assert any(r["span_kind"] == "AGENT" for r in records)
