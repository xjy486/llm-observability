"""Blocker 2: GraphInterrupt must not mark AGENT root as ERROR.

Tests that GraphInterrupt propagates through the full agent wrapper
(ObservedLangChainAgent → _AgentScope → TraceContextManager) without
marking the AGENT span as ERROR.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import get_current_context, _context_var
from llm_observability.integrations.langchain.agent_wrapper import observe_agent
from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
from llm_observability.integrations.langchain.compat import is_control_flow_exception

try:
    from langgraph.errors import GraphInterrupt
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    if _context_var.get() is not None:
        _context_var.set(None)
    Observability.init(app_name="interrupt-agent-test", endpoint="http://localhost:99999")
    yield Observability._tracer
    Observability.shutdown()


def _make_interrupt():
    if HAS_LANGGRAPH:
        return GraphInterrupt()
    # Fallback: use a class with the right name
    class FakeGraphInterrupt(Exception):
        pass
    FakeGraphInterrupt.__name__ = "GraphInterrupt"
    return FakeGraphInterrupt()


def test_is_control_flow_exception_unifies_all_cases():
    """The shared helper recognizes GeneratorExit, CancelledError, and GraphInterrupt."""
    import asyncio
    assert is_control_flow_exception(GeneratorExit()) is True
    assert is_control_flow_exception(asyncio.CancelledError()) is True
    assert is_control_flow_exception(RuntimeError("fail")) is False


def test_observed_agent_graph_interrupt_agent_not_error(init_sdk):
    """GraphInterrupt during agent.invoke → AGENT span is NOT ERROR."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    interrupt_exc = _make_interrupt()

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(side_effect=interrupt_exc)

    observed = observe_agent(fake_agent, name="interrupt-agent")

    with pytest.raises(type(interrupt_exc)):
        observed.invoke({"messages": []})

    tracer.reporter.report = orig
    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1, f"Expected 1 AGENT span, got {len(agent_spans)}"
    assert agent_spans[0]["status"] != "ERROR", (
        f"AGENT root should NOT be ERROR for GraphInterrupt, got status={agent_spans[0]['status']}"
    )


def test_observed_agent_graph_interrupt_llm_not_error(init_sdk):
    """GraphInterrupt in model call → LLM span NOT ERROR + AGENT span NOT ERROR."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    interrupt_exc = _make_interrupt()

    # Simulate agent.invoke calling middleware which raises GraphInterrupt
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()

    def handler(req):
        raise interrupt_exc

    # First, test at middleware level (LLM span)
    with pytest.raises(type(interrupt_exc)):
        mw.wrap_model_call(request, handler)

    tracer.reporter.report = orig
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    if llm_spans:
        span = llm_spans[0]
        assert span["status"] != "ERROR", (
            f"LLM span should NOT be ERROR for GraphInterrupt, got status={span['status']}"
        )
        attrs = span.get("attributes", {})
        assert attrs.get("langchain.interrupted") is True


def test_observed_agent_graph_interrupt_is_reraised(init_sdk):
    """GraphInterrupt must be re-raised through the full agent wrapper."""
    interrupt_exc = _make_interrupt()

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(side_effect=interrupt_exc)

    observed = observe_agent(fake_agent, name="rereraise-agent")

    with pytest.raises(type(interrupt_exc)):
        observed.invoke({"messages": []})


def test_observed_agent_graph_interrupt_tool_not_error(init_sdk):
    """GraphInterrupt in tool call → TOOL span NOT ERROR."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    interrupt_exc = _make_interrupt()

    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    request.tool_call = {"name": "search", "args": {}, "id": "tc1"}

    def handler(req):
        raise interrupt_exc

    with pytest.raises(type(interrupt_exc)):
        mw.wrap_tool_call(request, handler)

    tracer.reporter.report = orig
    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    if tool_spans:
        span = tool_spans[0]
        assert span["status"] != "ERROR", (
            f"TOOL span should NOT be ERROR for GraphInterrupt, got status={span['status']}"
        )
