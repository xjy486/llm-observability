"""Phase 2.3: Integration test with real create_agent.

Uses Fake Chat Model + real LangChain Tool + real Middleware Runtime
to verify the complete trace structure.
"""
import pytest
import asyncio
from typing import ClassVar
from llm_observability import Observability
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeChatModelWithToolCall(BaseChatModel):
    """Fake model that calls a tool on first invocation, then responds."""
    call_count: ClassVar[int] = 0

    @property
    def _llm_type(self):
        return "fake_tool"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        FakeChatModelWithToolCall.call_count += 1
        if FakeChatModelWithToolCall.call_count == 1:
            ai_msg = AIMessage(
                content="",
                tool_calls=[{"name": "calculator", "args": {"x": 5, "y": 3}, "id": "call_1", "type": "tool_call"}],
            )
        else:
            ai_msg = AIMessage(content="The answer is 8!")
        return ChatResult(generations=[ChatGeneration(message=ai_msg)])

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def calculator(x: int, y: int) -> int:
    """Add two numbers."""
    return x + y


@pytest.fixture
def init_sdk():
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    yield
    Observability.shutdown()


def test_full_agent_loop_trace_structure(init_sdk):
    """Verify AGENT -> LLM -> TOOL -> LLM trace structure."""
    from llm_observability.integrations.langchain import observe_agent, LangChainObservabilityMiddleware

    FakeChatModelWithToolCall.call_count = 0
    agent = create_agent(
        model=FakeChatModelWithToolCall(),
        tools=[calculator],
        middleware=[LangChainObservabilityMiddleware()],
    )
    observed = observe_agent(agent, name="math-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    result = observed.invoke({"messages": [HumanMessage(content="What is 5+3?")]})

    Observability._tracer.reporter.report = original_report

    # Verify trace structure
    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    llm_spans = [s for s in captured_spans if s["span_kind"] == "LLM"]
    tool_spans = [s for s in captured_spans if s["span_kind"] == "TOOL"]

    assert len(agent_spans) == 1, f"Expected 1 AGENT span, got {len(agent_spans)}"
    assert len(llm_spans) == 2, f"Expected 2 LLM spans, got {len(llm_spans)}"
    assert len(tool_spans) == 1, f"Expected 1 TOOL span, got {len(tool_spans)}"

    # Verify all share the same trace_id
    trace_ids = {s["trace_id"] for s in captured_spans}
    assert len(trace_ids) == 1, f"Expected 1 trace_id, got {trace_ids}"

    # Verify parent relationships
    agent_span_id = agent_spans[0]["span_id"]

    # All LLM and TOOL spans must have parent = AGENT
    for s in llm_spans + tool_spans:
        assert s["parent_span_id"] == agent_span_id, \
            f"Span {s['span_name']} has wrong parent: {s['parent_span_id']} != {agent_span_id}"

    # Verify tool name and call_id
    assert tool_spans[0]["attributes"].get("tool.name") == "calculator"
    assert tool_spans[0]["attributes"].get("tool.call_id") == "call_1"

    # Verify framework metadata
    assert llm_spans[0]["attributes"].get("framework.name") == "langchain"
    assert tool_spans[0]["attributes"].get("framework.name") == "langchain"

    # Verify result
    assert len(result["messages"]) == 4  # human, ai(tool_call), tool, ai(final)


def test_full_agent_loop_stream_trace(init_sdk):
    """Verify streaming produces correct trace structure."""
    from llm_observability.integrations.langchain import observe_agent, LangChainObservabilityMiddleware

    FakeChatModelWithToolCall.call_count = 0
    agent = create_agent(
        model=FakeChatModelWithToolCall(),
        tools=[calculator],
        middleware=[LangChainObservabilityMiddleware()],
    )
    observed = observe_agent(agent, name="stream-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    chunks = list(observed.stream({"messages": [HumanMessage(content="What is 5+3?")]})
)

    Observability._tracer.reporter.report = original_report

    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    assert agent_spans[0]["status"] == "OK"


def test_agent_with_middleware_no_wrapper_no_isolated_spans(init_sdk):
    """Middleware without active trace should not produce isolated spans."""
    from llm_observability.integrations.langchain import LangChainObservabilityMiddleware

    FakeChatModelWithToolCall.call_count = 0
    agent = create_agent(
        model=FakeChatModelWithToolCall(),
        tools=[calculator],
        middleware=[LangChainObservabilityMiddleware()],
    )
    # Note: NOT wrapping with observe_agent — no active trace

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    result = agent.invoke({"messages": [HumanMessage(content="What is 5+3?")]})

    Observability._tracer.reporter.report = original_report

    # No spans should be produced (no active trace)
    assert len(captured_spans) == 0
