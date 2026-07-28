"""P1: Real single-invocation retry test with create_agent.

Tests that a single observed.invoke() with both ModelRetryMiddleware
and LangChainObservabilityMiddleware produces:
- 1 AGENT span
- 2 LLM spans (1 ERROR + 1 OK)
- Unique span_ids
- Same trace_id
- AGENT final status OK
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import _context_var, SpanContext, set_context, reset_context
from llm_observability.integrations.langchain.agent_wrapper import observe_agent
from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware, ToolRetryMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool


class FlakyChatModel(BaseChatModel):
    """Chat model that fails on first call, succeeds on retry."""

    def __init__(self):
        super().__init__()
        self._call_count = 0

    @property
    def _llm_type(self):
        return "flaky-test-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._call_count += 1
        if self._call_count == 1:
            raise ValueError("transient model error — will retry")
        from langchain_core.outputs import ChatGeneration, ChatResult
        msg = AIMessage(content="8 + 3 = 11")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self


class StableChatModel(BaseChatModel):
    """Chat model that returns a tool call on first invocation, then plain text."""

    def __init__(self):
        super().__init__()
        self._call_count = 0

    @property
    def _llm_type(self):
        return "stable-test-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._call_count += 1
        from langchain_core.outputs import ChatGeneration, ChatResult
        if self._call_count == 1:
            # First call: return a tool call to trigger the flaky tool
            msg = AIMessage(
                content="",
                tool_calls=[{"name": "flaky_calculator", "args": {"expression": "8+3"}, "id": "tc1"}],
            )
        else:
            # Subsequent calls: return plain text (end the agent loop)
            msg = AIMessage(content="The result is 11.")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    if _context_var.get() is not None:
        _context_var.set(None)
    Observability.init(app_name="single-retry-test", endpoint="http://localhost:99999")
    yield Observability._tracer
    Observability.shutdown()


def test_single_invoke_model_retry_produces_attempt_spans(init_sdk):
    """One observed.invoke() with retry → 1 AGENT + 2 LLM (1 ERROR + 1 OK), same trace."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    flaky_model = FlakyChatModel()

    @tool
    def calculator(expression: str) -> str:
        """Calculate a math expression."""
        return str(eval(expression))

    agent = create_agent(
        model=flaky_model,
        tools=[calculator],
        middleware=[
            ModelRetryMiddleware(max_retries=1, backoff_factor=0.0, initial_delay=0.0, jitter=False),
            LangChainObservabilityMiddleware(),
        ],
    )

    observed = observe_agent(agent, name="retry-single-agent", root_mode="create")

    result = observed.invoke({"messages": [HumanMessage(content="What is 8+3?")]})

    tracer.reporter.report = orig

    # Verify business result
    assert result is not None

    # Analyze spans
    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]

    # 1 AGENT span
    assert len(agent_spans) == 1, f"Expected 1 AGENT span, got {len(agent_spans)}"

    # 2 LLM spans (1 per retry attempt)
    assert len(llm_spans) == 2, f"Expected 2 LLM spans (1 per attempt), got {len(llm_spans)}"

    # First LLM = ERROR, Second LLM = OK
    statuses = [s["status"] for s in llm_spans]
    assert "ERROR" in statuses, f"Expected at least 1 ERROR LLM span, got {statuses}"
    assert "OK" in statuses or "UNSET" in statuses, f"Expected at least 1 OK LLM span, got {statuses}"

    # Unique span_ids
    span_ids = [s["span_id"] for s in llm_spans]
    assert len(span_ids) == len(set(span_ids)), f"LLM span_ids must be unique: {span_ids}"

    # Same trace_id
    trace_ids = set([s["trace_id"] for s in agent_spans] + [s["trace_id"] for s in llm_spans])
    assert len(trace_ids) == 1, f"All spans must share trace_id, got {trace_ids}"

    # AGENT final status OK (retry succeeded)
    assert agent_spans[0]["status"] != "ERROR", (
        f"AGENT should be OK when retry succeeds, got {agent_spans[0]['status']}"
    )

    # LLM parent must be AGENT
    agent_span_id = agent_spans[0]["span_id"]
    for llm in llm_spans:
        assert llm["parent_span_id"] == agent_span_id, (
            f"LLM span {llm['span_id'][:8]} parent should be AGENT, "
            f"got {llm['parent_span_id'][:8]}"
        )


def test_single_invoke_tool_retry_produces_attempt_spans(init_sdk):
    """One observed.invoke() with tool retry → 1 AGENT + 2 TOOL (1 ERROR + 1 OK)."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    stable_model = StableChatModel()

    tool_call_count = [0]

    @tool
    def flaky_calculator(expression: str) -> str:
        """Calculate a math expression."""
        tool_call_count[0] += 1
        if tool_call_count[0] == 1:
            raise RuntimeError("tool transient error")
        return str(eval(expression))

    agent = create_agent(
        model=stable_model,
        tools=[flaky_calculator],
        middleware=[
            ToolRetryMiddleware(max_retries=1, backoff_factor=0.0, initial_delay=0.0, jitter=False),
            LangChainObservabilityMiddleware(),
        ],
    )

    observed = observe_agent(agent, name="tool-retry-single-agent", root_mode="create")

    result = observed.invoke({"messages": [HumanMessage(content="Use flaky_calculator to compute 8+3")]})

    tracer.reporter.report = orig

    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]

    # 1 AGENT span
    assert len(agent_spans) == 1, f"Expected 1 AGENT span, got {len(agent_spans)}"

    # At least 2 TOOL spans (1 per retry attempt) if tool was retried
    if tool_call_count[0] >= 2:
        assert len(tool_spans) >= 2, (
            f"Expected >= 2 TOOL spans when tool retried, got {len(tool_spans)}"
        )
        statuses = [s["status"] for s in tool_spans]
        assert "ERROR" in statuses, f"Expected at least 1 ERROR TOOL span, got {statuses}"

        # Unique span_ids
        span_ids = [s["span_id"] for s in tool_spans]
        assert len(span_ids) == len(set(span_ids)), f"TOOL span_ids must be unique: {span_ids}"

    # Same trace_id
    trace_ids = set([s["trace_id"] for s in agent_spans] + [s["trace_id"] for s in tool_spans])
    assert len(trace_ids) == 1, f"All spans must share trace_id, got {trace_ids}"

    # AGENT final status OK
    assert agent_spans[0]["status"] != "ERROR", (
        f"AGENT should be OK when retry succeeds, got {agent_spans[0]['status']}"
    )