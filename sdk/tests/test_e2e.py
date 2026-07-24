"""End-to-end integration tests for SDK → Proxy → Core.

Tests spec §29 scenarios using mock Core.

P0-1: Tests use Public API only — no manual reporter.start().
P0-2: Tests use instance-level _original_create patching.
"""
import sys
import os
import asyncio
import json
import threading
import time
from unittest.mock import MagicMock, patch
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "proxy"))

import pytest
from llm_observability import Observability
from llm_observability.context import get_current_context
from llm_observability.propagation import inject_traceparent, extract_traceparent
from llm_observability.instrumentation.openai import OpenAIInstrumentor
from trace_context import resolve_trace_context, extract_ownership

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


# ─── Mock Core Server ──────────────────────────────────────

class MockCoreHandler(BaseHTTPRequestHandler):
    """Mock Observability Core that stores ingested spans."""
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
    """Start a mock Core server and yield its URL."""
    MockCoreHandler.received_records = []
    server = HTTPServer(("127.0.0.1", 0), MockCoreHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def reset_sdk():
    """Ensure SDK is reset between tests."""
    yield
    if Observability._initialized:
        Observability.shutdown()


def _make_fake_response():
    """Create a fake OpenAI ChatCompletion response object."""
    fake_response = MagicMock()
    fake_response.model = "gpt-4"
    fake_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    fake_response.model_dump.return_value = {
        "id": "chatcmpl-123",
        "model": "gpt-4",
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    return fake_response


# ─── Tests ─────────────────────────────────────────────────


@pytest.mark.skipif(not HAS_OPENAI, reason="openai not installed")
def test_scenario_b_sdk_single_llm(mock_core):
    """Scenario B: SDK Trace → OpenAI → Proxy → GATEWAY span.

    P0-1: Uses Public API only — init() auto-starts the reporter.
    Asserts: TraceID same, AGENT parent=None, LLM parent=AGENT.
    """
    # P0-1: init() auto-starts reporter — no manual start needed
    Observability.init(
        app_name="test-app",
        endpoint=mock_core,
        auto_instrument_openai=False,
    )
    tracer = Observability._tracer
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()

    # P0-2: Patch instance-level _original_create
    with patch.object(instr, "_original_create", return_value=fake_response) as mock_orig:
        with tracer.trace(name="demo-task"):
            client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
            client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "hi"}])

    # Wait for async flush
    time.sleep(1.0)

    instr.uninstrument()
    Observability.shutdown()

    records = MockCoreHandler.received_records
    # Should have AGENT + LLM
    kinds = [r["span_kind"] for r in records]
    assert "AGENT" in kinds
    assert "LLM" in kinds

    agent = [r for r in records if r["span_kind"] == "AGENT"][0]
    llm = [r for r in records if r["span_kind"] == "LLM"][0]

    assert agent["trace_id"] == llm["trace_id"]
    assert agent["parent_span_id"] is None
    assert llm["parent_span_id"] == agent["span_id"]


def test_scenario_a_no_sdk_compatible():
    """Scenario A: No SDK → Proxy still creates root LLM trace."""
    headers = {}
    ctx = resolve_trace_context(headers)
    ownership = extract_ownership(headers)

    assert ctx.inherited is False  # new root trace
    assert ownership is None  # no marker → LLM fallback


@pytest.mark.skipif(not HAS_OPENAI, reason="openai not installed")
def test_scenario_c_multi_llm_same_trace(mock_core):
    """Scenario C: Multiple LLM calls in one trace, same TraceID.

    P0-1: Uses Public API only.
    """
    Observability.init(
        app_name="test-app",
        endpoint=mock_core,
        auto_instrument_openai=False,
    )
    tracer = Observability._tracer
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()

    with patch.object(instr, "_original_create", return_value=fake_response) as mock_orig:
        with tracer.trace(name="multi-llm-task"):
            client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
            client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "1"}])
            client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "2"}])

    time.sleep(1.0)

    instr.uninstrument()
    Observability.shutdown()

    records = MockCoreHandler.received_records
    llm_records = [r for r in records if r["span_kind"] == "LLM"]
    assert len(llm_records) == 2
    assert llm_records[0]["trace_id"] == llm_records[1]["trace_id"]
    assert llm_records[0]["span_id"] != llm_records[1]["span_id"]


@pytest.mark.skipif(not HAS_OPENAI, reason="openai not installed")
def test_reporter_failure_does_not_block_business():
    """Spec §29.6: Reporter failure does not affect business call.

    P0-1: Reporter auto-started, failures are fail-open.
    """
    Observability.init(
        app_name="test-app",
        endpoint="http://localhost:1",  # unreachable
        auto_instrument_openai=False,
    )
    tracer = Observability._tracer
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()

    call_succeeded = False
    with patch.object(instr, "_original_create", return_value=fake_response) as mock_orig:
        with tracer.trace(name="fail-open-test"):
            client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
            result = client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "hi"}])
            call_succeeded = result is not None

    instr.uninstrument()
    Observability.shutdown()

    assert call_succeeded, "Business call should succeed even with reporter failure"


@pytest.mark.skipif(not HAS_OPENAI, reason="openai not installed")
def test_duplicate_llm_dedup():
    """Spec §29.7: One OpenAI call = one logical LLM span.

    P1-4: Dedup still propagates traceparent but creates no new span.
    """
    Observability.init(
        app_name="test-app",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )
    tracer = Observability._tracer
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()

    with patch.object(instr, "_original_create", return_value=fake_response) as mock_orig:
        with tracer.trace(name="dedup-test"):
            client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
            client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "hi"}])

    instr.uninstrument()
    Observability.shutdown()

    records = list(tracer.reporter._queue)
    llm_count = sum(1 for r in records if r["span_kind"] == "LLM")
    assert llm_count == 1, "Should have exactly one LLM span per OpenAI call"
