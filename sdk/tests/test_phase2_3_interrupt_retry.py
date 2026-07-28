"""P1-2/P1-3: Interrupt and Retry tests."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock, patch
from llm_observability import Observability
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context
from llm_observability.integrations.langchain.compat import is_langgraph_interrupt
from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan

try:
    from langgraph.errors import GraphInterrupt
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="interrupt-test", endpoint="http://localhost:99999")
    ctx = SpanContext(trace_id="0"*32, span_id="a"*16, parent_span_id=None, span_kind="AGENT", sampled=True)
    token = set_context(ctx)
    yield Observability._tracer
    reset_context(token)
    Observability.shutdown()


def test_is_langgraph_interrupt_recognizes_graphinterrupt():
    if not HAS_LANGGRAPH:
        pytest.skip("langgraph not installed")
    assert is_langgraph_interrupt(GraphInterrupt()) is True


def test_is_langgraph_interrupt_rejects_runtime_error():
    assert is_langgraph_interrupt(RuntimeError("fail")) is False


def test_graph_interrupt_llm_span_not_error(init_sdk):
    """GraphInterrupt in model call → LLM span NOT marked ERROR, interrupted=true."""
    tracer = init_sdk
    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    if HAS_LANGGRAPH:
        exc = GraphInterrupt()
    else:
        exc = RuntimeError("interrupt")

    handler = MagicMock(side_effect=exc)
    with pytest.raises(Exception):
        mw.wrap_model_call(request, handler)

    tracer.reporter.report = orig_report
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    if llm_spans:
        span = llm_spans[0]
        assert span["status"] != "ERROR", f"Interrupt should not be ERROR, got {span['status']}"
        attrs = span.get("attributes", {})
        assert attrs.get("langchain.interrupted") is True


def test_graph_interrupt_is_reraised(init_sdk):
    """GraphInterrupt must be re-raised, not swallowed."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    if HAS_LANGGRAPH:
        exc = GraphInterrupt()
    else:
        exc = RuntimeError("interrupt")
    handler = MagicMock(side_effect=exc)
    with pytest.raises(type(exc)):
        mw.wrap_model_call(request, handler)


def test_normal_runtime_error_still_marks_error(init_sdk):
    """Normal exceptions are still marked as ERROR."""
    tracer = init_sdk
    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    handler = MagicMock(side_effect=ValueError("real error"))
    with pytest.raises(ValueError):
        mw.wrap_model_call(request, handler)

    tracer.reporter.report = orig_report
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    assert len(llm_spans) >= 1
    assert llm_spans[0]["status"] == "ERROR"
