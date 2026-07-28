"""Blocker 3: Real Retry Middleware tests with attempt-per-span.

Tests that when LangChainObservabilityMiddleware is composed BELOW
ModelRetryMiddleware / ToolRetryMiddleware (i.e., Retry is outermost),
each retry attempt creates a separate LLM/TOOL span with correct
status and unique span_id.

Recommended middleware order (outermost → innermost):
  [ModelRetryMiddleware, LangChainObservabilityMiddleware]

This way Retry calls handler(request) N times, and each call enters
the Observability wrapper, creating one span per attempt.
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context
from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware

from langchain.agents.middleware import (
    ModelRetryMiddleware,
    ToolRetryMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
    Runtime,
)
from langgraph.runtime import ExecutionInfo
from langchain_core.messages import AIMessage, ToolMessage


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="retry-test", endpoint="http://localhost:99999")
    ctx = SpanContext(
        trace_id="0" * 32, span_id="a" * 16,
        parent_span_id=None, span_kind="AGENT", sampled=True,
    )
    token = set_context(ctx)
    yield Observability._tracer
    reset_context(token)
    Observability.shutdown()


def _make_model_request(attempt=1):
    """Create a realistic ModelRequest with runtime.execution_info.node_attempt."""
    request = MagicMock(spec=ModelRequest)
    request.model = MagicMock()
    request.model.model_name = "gpt-4"
    request.messages = []
    request.system_message = None
    request.tool_choice = None
    request.tools = []
    request.response_format = None
    request.state = MagicMock()
    request.model_settings = {}

    runtime = MagicMock(spec=Runtime)
    runtime.execution_info = ExecutionInfo(
        checkpoint_id="cp1",
        checkpoint_ns="ns",
        task_id="task1",
        thread_id="t1",
        run_id="r1",
        node_attempt=attempt,
    )
    request.runtime = runtime
    return request


def _make_tool_request():
    request = MagicMock(spec=ToolCallRequest)
    request.tool_call = {"name": "search", "args": {"q": "test"}, "id": "tc1"}
    request.tool = MagicMock()
    request.tool.name = "search"
    request.state = MagicMock()

    runtime = MagicMock(spec=Runtime)
    runtime.execution_info = ExecutionInfo(
        checkpoint_id="cp1",
        checkpoint_ns="ns",
        task_id="task1",
        thread_id="t1",
        run_id="r1",
        node_attempt=1,
    )
    request.runtime = runtime
    return request


def test_real_model_retry_creates_span_per_attempt(init_sdk):
    """ModelRetryMiddleware with max_retries=1 → 2 LLM spans (1 ERROR + 1 OK)."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    obs_mw = LangChainObservabilityMiddleware()
    retry_mw = ModelRetryMiddleware(
        max_retries=1,
        backoff_factor=0.0,
        initial_delay=0.0,
        jitter=False,
    )

    call_count = [0]

    def actual_model_call(request):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("transient error")
        return ModelResponse(result=[AIMessage(content="success")])

    # Compose: retry_mw wraps obs_mw wraps actual_model_call
    # retry_mw.wrap_model_call(request, obs_handler)
    #   where obs_handler = obs_mw.wrap_model_call(request, actual_handler)
    def obs_handler(request):
        return obs_mw.wrap_model_call(request, actual_model_call)

    request = _make_model_request(attempt=1)
    result = retry_mw.wrap_model_call(request, obs_handler)

    tracer.reporter.report = orig

    assert call_count[0] == 2, f"Expected 2 model calls, got {call_count[0]}"
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    assert len(llm_spans) == 2, f"Expected 2 LLM spans (1 per attempt), got {len(llm_spans)}"

    # First attempt should be ERROR, second should be OK
    statuses = [s["status"] for s in llm_spans]
    assert "ERROR" in statuses, f"Expected at least 1 ERROR span, got {statuses}"
    assert "OK" in statuses or "UNSET" in statuses, f"Expected at least 1 OK span, got {statuses}"


def test_real_tool_retry_creates_span_per_attempt(init_sdk):
    """ToolRetryMiddleware with max_retries=1 → 2 TOOL spans (1 ERROR + 1 OK)."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    obs_mw = LangChainObservabilityMiddleware()
    retry_mw = ToolRetryMiddleware(
        max_retries=1,
        backoff_factor=0.0,
        initial_delay=0.0,
        jitter=False,
    )

    call_count = [0]

    def actual_tool_call(request):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("tool transient error")
        return ToolMessage(content="tool result", tool_call_id="tc1")

    def obs_tool_handler(request):
        return obs_mw.wrap_tool_call(request, actual_tool_call)

    request = _make_tool_request()
    result = retry_mw.wrap_tool_call(request, obs_tool_handler)

    tracer.reporter.report = orig

    assert call_count[0] == 2, f"Expected 2 tool calls, got {call_count[0]}"
    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    assert len(tool_spans) == 2, f"Expected 2 TOOL spans (1 per attempt), got {len(tool_spans)}"

    statuses = [s["status"] for s in tool_spans]
    assert "ERROR" in statuses, f"Expected at least 1 ERROR span, got {statuses}"
    assert "OK" in statuses or "UNSET" in statuses, f"Expected at least 1 OK span, got {statuses}"


def test_retry_attempts_have_unique_span_ids(init_sdk):
    """Each retry attempt must have a unique span_id."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    obs_mw = LangChainObservabilityMiddleware()
    retry_mw = ModelRetryMiddleware(
        max_retries=1,
        backoff_factor=0.0,
        initial_delay=0.0,
        jitter=False,
    )

    call_count = [0]

    def actual_model_call(request):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("fail")
        return ModelResponse(result=[AIMessage(content="ok")])

    def obs_handler(request):
        return obs_mw.wrap_model_call(request, actual_model_call)

    request = _make_model_request(attempt=1)
    retry_mw.wrap_model_call(request, obs_handler)

    tracer.reporter.report = orig
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    span_ids = [s["span_id"] for s in llm_spans]
    assert len(span_ids) == len(set(span_ids)), f"Span IDs must be unique, got {span_ids}"


def test_retry_failed_attempt_is_error(init_sdk):
    """A failed attempt (that gets retried) must be marked ERROR."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    obs_mw = LangChainObservabilityMiddleware()
    retry_mw = ModelRetryMiddleware(
        max_retries=1,
        backoff_factor=0.0,
        initial_delay=0.0,
        jitter=False,
    )

    call_count = [0]

    def actual_model_call(request):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("first attempt fails")
        return ModelResponse(result=[AIMessage(content="recovered")])

    def obs_handler(request):
        return obs_mw.wrap_model_call(request, actual_model_call)

    request = _make_model_request(attempt=1)
    retry_mw.wrap_model_call(request, obs_handler)

    tracer.reporter.report = orig
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    assert len(llm_spans) >= 2
    error_spans = [s for s in llm_spans if s["status"] == "ERROR"]
    assert len(error_spans) >= 1, "Failed attempt must be ERROR"


def test_retry_success_attempt_is_ok(init_sdk):
    """The successful retry attempt must be marked OK."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    obs_mw = LangChainObservabilityMiddleware()
    retry_mw = ModelRetryMiddleware(
        max_retries=1,
        backoff_factor=0.0,
        initial_delay=0.0,
        jitter=False,
    )

    call_count = [0]

    def actual_model_call(request):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("fail")
        return ModelResponse(result=[AIMessage(content="ok")])

    def obs_handler(request):
        return obs_mw.wrap_model_call(request, actual_model_call)

    request = _make_model_request(attempt=1)
    retry_mw.wrap_model_call(request, obs_handler)

    tracer.reporter.report = orig
    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    ok_spans = [s for s in llm_spans if s["status"] in ("OK", "UNSET")]
    assert len(ok_spans) >= 1, "Successful attempt must be OK/UNSET"


def test_retry_final_agent_status_is_ok(init_sdk):
    """When retry succeeds, the overall agent trace should not be ERROR.

    Simulates a middleware-wrapped agent where the first model call fails
    but retry succeeds. The AGENT root span should be OK.
    """
    from llm_observability.integrations.langchain.agent_wrapper import observe_agent
    from llm_observability.context import _context_var

    if _context_var.get() is not None:
        _context_var.set(None)

    # Reset context to None so agent creates its own trace
    current = get_current_context()
    if current is not None:
        reset_context(set_context(None))

    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    call_count = [0]

    def fake_invoke(input, config=None, **kwargs):
        call_count[0] += 1
        # Simulate: first call fails, second call (retry) succeeds
        if call_count[0] == 1:
            raise ValueError("transient")
        return {"result": "success after retry"}

    fake_agent = MagicMock()
    fake_agent.invoke = fake_invoke

    observed = observe_agent(fake_agent, name="retry-agent")

    # First invoke fails
    with pytest.raises(ValueError):
        observed.invoke({"messages": []})

    # Second invoke (retry) succeeds
    result = observed.invoke({"messages": []})

    tracer.reporter.report = orig
    assert result == {"result": "success after retry"}

    # The second AGENT span (successful) should be OK
    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    assert len(agent_spans) >= 2, f"Expected >= 2 AGENT spans, got {len(agent_spans)}"
    last_agent = agent_spans[-1]
    assert last_agent["status"] != "ERROR", (
        f"AGENT should not be ERROR when business succeeds, got {last_agent['status']}"
    )