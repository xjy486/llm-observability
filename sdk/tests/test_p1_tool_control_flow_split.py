"""P1: Tool control flow vs HITL interrupt split tests.

Verifies that:
- GeneratorExit and CancelledError do NOT set langchain.interrupted
- Only GraphInterrupt/NodeInterrupt set langchain.interrupted=true
- All control flow exceptions do NOT set ERROR
"""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import SpanContext, set_context, reset_context
from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware

try:
    from langgraph.errors import GraphInterrupt
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="tool-cf-split-test", endpoint="http://localhost:99999")
    ctx = SpanContext(
        trace_id="0" * 32, span_id="a" * 16,
        parent_span_id=None, span_kind="AGENT", sampled=True,
    )
    token = set_context(ctx)
    yield Observability._tracer
    reset_context(token)
    Observability.shutdown()


def _make_tool_request():
    request = MagicMock()
    request.tool_call = {"name": "search", "args": {}, "id": "tc1"}
    return request


def test_generator_exit_tool_not_error_and_not_interrupt(init_sdk):
    """GeneratorExit in tool → no ERROR, no langchain.interrupted."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = _make_tool_request()

    def handler(req):
        raise GeneratorExit()

    with pytest.raises(GeneratorExit):
        mw.wrap_tool_call(request, handler)

    tracer.reporter.report = orig
    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    assert len(tool_spans) == 1
    span = tool_spans[0]

    # No ERROR
    assert span["status"] != "ERROR", (
        f"GeneratorExit should not be ERROR, got {span['status']}"
    )

    # No langchain.interrupted
    attrs = span.get("attributes", {})
    assert attrs.get("langchain.interrupted") is not True, (
        "GeneratorExit should NOT set langchain.interrupted=true"
    )
    assert "langchain.interrupt.type" not in attrs, (
        "GeneratorExit should NOT set langchain.interrupt.type"
    )


def test_cancelled_error_tool_not_error_and_not_interrupt(init_sdk):
    """CancelledError in tool → no ERROR, no langchain.interrupted."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = _make_tool_request()

    def handler(req):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        mw.wrap_tool_call(request, handler)

    tracer.reporter.report = orig
    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    assert len(tool_spans) == 1
    span = tool_spans[0]

    assert span["status"] != "ERROR", (
        f"CancelledError should not be ERROR, got {span['status']}"
    )

    attrs = span.get("attributes", {})
    assert attrs.get("langchain.interrupted") is not True, (
        "CancelledError should NOT set langchain.interrupted=true"
    )


def test_graph_interrupt_tool_sets_interrupt_flag(init_sdk):
    """GraphInterrupt in tool → no ERROR, but langchain.interrupted=true."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = _make_tool_request()

    if HAS_LANGGRAPH:
        exc = GraphInterrupt()
    else:
        class FakeGraphInterrupt(Exception):
            pass
        FakeGraphInterrupt.__name__ = "GraphInterrupt"
        exc = FakeGraphInterrupt()

    def handler(req):
        raise exc

    with pytest.raises(type(exc)):
        mw.wrap_tool_call(request, handler)

    tracer.reporter.report = orig
    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    assert len(tool_spans) == 1
    span = tool_spans[0]

    # No ERROR
    assert span["status"] != "ERROR", (
        f"GraphInterrupt should not be ERROR, got {span['status']}"
    )

    # langchain.interrupted=true (ONLY for GraphInterrupt)
    attrs = span.get("attributes", {})
    assert attrs.get("langchain.interrupted") is True, (
        "GraphInterrupt should set langchain.interrupted=true"
    )
    assert "langchain.interrupt.type" in attrs, (
        "GraphInterrupt should set langchain.interrupt.type"
    )


def test_runtime_error_tool_is_error_and_not_interrupt(init_sdk):
    """RuntimeError in tool → ERROR, no langchain.interrupted."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = _make_tool_request()

    def handler(req):
        raise RuntimeError("business error")

    with pytest.raises(RuntimeError):
        mw.wrap_tool_call(request, handler)

    tracer.reporter.report = orig
    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    assert len(tool_spans) == 1
    span = tool_spans[0]

    # ERROR
    assert span["status"] == "ERROR", (
        f"RuntimeError should be ERROR, got {span['status']}"
    )

    # No langchain.interrupted
    attrs = span.get("attributes", {})
    assert attrs.get("langchain.interrupted") is not True, (
        "RuntimeError should NOT set langchain.interrupted"
    )
