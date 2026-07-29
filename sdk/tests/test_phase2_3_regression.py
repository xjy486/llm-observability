"""Phase 2.3 Regression: Phase 2.1 / 2.2 compatibility.

Verifies that adding the LangChain integration does NOT break:
- Phase 2.1: OpenAI Instrumentor + AGENT/LLM/GATEWAY span tree
- Phase 2.2: Manual Tool span API
- OpenAI dedup flag still works when LangChain is not involved
- Core SDK imports & works without LangChain-specific calls
"""
import sys
import os
import asyncio
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from llm_observability import Observability
from llm_observability.context import get_current_context, SpanContext, set_context, reset_context
from llm_observability.instrumentation.openai import OpenAIInstrumentor
from llm_observability.utils.ids import generate_trace_id, generate_span_id

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

pytestmark = pytest.mark.skipif(not HAS_OPENAI, reason="openai not installed")


@pytest.fixture
def init_sdk():
    """Init SDK with OpenAI auto-instrumentation."""
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(
        app_name="regression-test",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )
    yield Observability._tracer
    Observability.shutdown()


def _make_fake_response():
    """Create a fake OpenAI ChatCompletion response."""
    fake_choice = MagicMock()
    fake_choice.message.content = "Hello!"
    fake_choice.message.role = "assistant"
    fake_choice.finish_reason = "stop"
    fake_choice.index = 0
    fake_resp = MagicMock()
    fake_resp.id = "chatcmpl-fake"
    fake_resp.model = "gpt-4o"
    fake_resp.choices = [fake_choice]
    fake_resp.usage = MagicMock()
    fake_resp.usage.prompt_tokens = 10
    fake_resp.usage.completion_tokens = 5
    fake_resp.usage.total_tokens = 15
    return fake_resp


# ─── Phase 2.1 Regression ────────────────────────────────────

def test_phase2_1_openai_llm_span_still_works(init_sdk):
    """Phase 2.1: OpenAI call under trace() still creates AGENT → LLM."""
    tracer = init_sdk
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()
    captured = []
    original_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    try:
        with patch.object(instr, "_original_create", return_value=fake_response):
            with tracer.trace(name="phase21-regression"):
                client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
                client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "hi"}],
                )
    finally:
        tracer.reporter.report = original_report
        instr.uninstrument()

    kinds = [r["span_kind"] for r in captured]
    assert "AGENT" in kinds, f"AGENT missing: {kinds}"
    assert "LLM" in kinds, f"LLM missing: {kinds}"

    agent = [r for r in captured if r["span_kind"] == "AGENT"][0]
    llm = [r for r in captured if r["span_kind"] == "LLM"][0]
    assert agent["trace_id"] == llm["trace_id"]
    assert agent["parent_span_id"] is None
    assert llm["parent_span_id"] == agent["span_id"]


def test_phase2_1_openai_dedup_flag_resets_without_langchain(init_sdk):
    """When LangChain middleware is NOT active, logical_llm_span_active must be False."""
    tracer = init_sdk
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()

    try:
        with patch.object(instr, "_original_create", return_value=fake_response):
            with tracer.trace(name="dedup-reset-test"):
                ctx = get_current_context()
                assert ctx.logical_llm_span_active is False, \
                    "logical_llm_span_active should be False without LangChain middleware"

                client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
                client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "hi"}],
                )

                ctx_after = get_current_context()
                assert ctx_after.logical_llm_span_active is False, \
                    "logical_llm_span_active should still be False after OpenAI call"
    finally:
        instr.uninstrument()


def test_phase2_1_nested_tool_still_works(init_sdk):
    """Phase 2.2: Manual Observability.tool() still creates TOOL span."""
    tracer = init_sdk
    captured = []
    original_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    try:
        with tracer.trace(name="tool-regression"):
            with tracer.tool(name="web_search", tool_type="search", input={"q": "test"}) as tool:
                tool.set_output({"results": ["a", "b"]})
    finally:
        tracer.reporter.report = original_report

    kinds = [r["span_kind"] for r in captured]
    assert "AGENT" in kinds
    assert "TOOL" in kinds

    agent = [r for r in captured if r["span_kind"] == "AGENT"][0]
    tool_s = [r for r in captured if r["span_kind"] == "TOOL"][0]
    assert tool_s["parent_span_id"] == agent["span_id"]
    assert tool_s["span_name"] == "tool.web_search"


def test_phase2_1_and_2_2_combined_unchanged(init_sdk):
    """Phase 2.1 + 2.2: trace + tool + openai still produces correct tree."""
    tracer = init_sdk
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()
    captured = []
    original_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    try:
        with patch.object(instr, "_original_create", return_value=fake_response):
            with tracer.trace(name="combined-regression"):
                client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
                client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "search"}],
                )
                with tracer.tool(name="lookup", tool_type="http", input={"url": "x"}) as t:
                    t.set_output({"data": "ok"})
                client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": "summarize"}],
                )
    finally:
        tracer.reporter.report = original_report
        instr.uninstrument()

    agent = [r for r in captured if r["span_kind"] == "AGENT"][0]
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]

    assert len(agent) > 0
    assert len(llm_spans) == 2, f"Expected 2 LLM spans, got {len(llm_spans)}"
    assert len(tool_spans) == 1, f"Expected 1 TOOL span, got {len(tool_spans)}"

    for s in llm_spans + tool_spans:
        assert s["parent_span_id"] == agent["span_id"], \
            f"{s['span_name']} parent wrong"

    trace_ids = {s["trace_id"] for s in captured}
    assert len(trace_ids) == 1, f"Expected 1 trace_id, got {trace_ids}"


# ─── Core SDK Regression ──────────────────────────────────────

def test_core_sdk_imports_without_langchain_calls():
    """Importing llm_observability must not require any langchain usage."""
    import llm_observability
    assert hasattr(llm_observability, "Observability")
    assert hasattr(llm_observability.Observability, "trace")
    assert hasattr(llm_observability.Observability, "tool")
    assert hasattr(llm_observability.Observability, "init")


def test_observability_init_without_langchain_dependency():
    """SDK init must work without any LangChain-related calls."""
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(
        app_name="no-langchain-test",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )
    assert Observability._initialized
    assert Observability._tracer is not None
    Observability.shutdown()
    assert not Observability._initialized


def test_langchain_middleware_convenience_requires_init():
    """langchain_middleware must raise if init() not called first."""
    if Observability._initialized:
        Observability.shutdown()
    with pytest.raises(RuntimeError, match="init"):
        Observability.langchain_middleware()


def test_instrument_langchain_agent_convenience_requires_init():
    """instrument_langchain_agent must raise if init() not called first."""
    if Observability._initialized:
        Observability.shutdown()
    with pytest.raises(RuntimeError, match="init"):
        Observability.instrument_langchain_agent(MagicMock())


# ─── SpanKind Constants Unchanged ─────────────────────────────

def test_span_kinds_unchanged():
    """Phase 2.3 must not add new SpanKind values (TASK added in Phase 2.5)."""
    from llm_observability.spans import SpanKind
    assert SpanKind.AGENT == "AGENT"
    assert SpanKind.LLM == "LLM"
    assert SpanKind.TOOL == "TOOL"
    assert SpanKind.GATEWAY == "GATEWAY"
    assert SpanKind.INTERNAL == "INTERNAL"
    # Phase 2.5 adds TASK SpanKind
    assert SpanKind.TASK == "TASK"
    members = [m for m in dir(SpanKind) if not m.startswith("_")]
    assert set(members) == {"AGENT", "LLM", "TOOL", "GATEWAY", "INTERNAL", "TASK"}