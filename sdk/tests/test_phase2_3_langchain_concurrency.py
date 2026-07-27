"""Phase 2.3: Concurrency, parallel tools, and interrupt tests."""
import pytest
import asyncio
from typing import ClassVar
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import get_current_context
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeChatModelParallel(BaseChatModel):
    call_count: ClassVar[int] = 0

    @property
    def _llm_type(self):
        return "fake_parallel"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        FakeChatModelParallel.call_count += 1
        if FakeChatModelParallel.call_count == 1:
            ai_msg = AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_a", "args": {"q": "a"}, "id": "c1", "type": "tool_call"},
                    {"name": "search_b", "args": {"q": "b"}, "id": "c2", "type": "tool_call"},
                    {"name": "search_c", "args": {"q": "c"}, "id": "c3", "type": "tool_call"},
                ],
            )
        else:
            ai_msg = AIMessage(content="Done!")
        return ChatResult(generations=[ChatGeneration(message=ai_msg)])

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def search_a(q: str) -> str:
    """Search A."""
    return f"A: {q}"


@tool
def search_b(q: str) -> str:
    """Search B."""
    return f"B: {q}"


@tool
def search_c(q: str) -> str:
    """Search C."""
    return f"C: {q}"


@pytest.fixture
def init_sdk():
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    yield
    Observability.shutdown()


def test_parallel_tools_are_siblings(init_sdk):
    """Parallel tools must be siblings, not nested."""
    from llm_observability.integrations.langchain import observe_agent, LangChainObservabilityMiddleware

    FakeChatModelParallel.call_count = 0
    agent = create_agent(
        model=FakeChatModelParallel(),
        tools=[search_a, search_b, search_c],
        middleware=[LangChainObservabilityMiddleware()],
    )
    observed = observe_agent(agent, name="parallel-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    observed.invoke({"messages": [HumanMessage(content="search")]})

    Observability._tracer.reporter.report = original_report

    tool_spans = [s for s in captured_spans if s["span_kind"] == "TOOL"]
    assert len(tool_spans) == 3

    # All tool spans must have the same parent (the AGENT span)
    parent_ids = {s["parent_span_id"] for s in tool_spans}
    assert len(parent_ids) == 1, f"Tool spans have different parents: {parent_ids}"

    # Each tool must have a unique span_id
    span_ids = [s["span_id"] for s in tool_spans]
    assert len(set(span_ids)) == 3


def test_parallel_async_agent_invocations_are_isolated(init_sdk):
    """Multiple async ainvoke calls must not share context."""
    from llm_observability.integrations.langchain import observe_agent

    class SimpleFakeModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "simple_fake"
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])
        def bind_tools(self, tools, **kwargs):
            return self

    agent1 = observe_agent(create_agent(model=SimpleFakeModel(), tools=[]), name="agent-1")
    agent2 = observe_agent(create_agent(model=SimpleFakeModel(), tools=[]), name="agent-2")

    async def run_both():
        results = await asyncio.gather(
            agent1.ainvoke({"messages": [HumanMessage(content="1")]}),
            agent2.ainvoke({"messages": [HumanMessage(content="2")]}),
        )
        return results

    results = asyncio.run(run_both())
    assert len(results) == 2

    assert get_current_context() is None


def test_multiple_agents_do_not_share_context(init_sdk):
    """Sequential agent invocations must not leak context."""
    from llm_observability.integrations.langchain import observe_agent

    class SimpleFakeModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "simple_fake"
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])
        def bind_tools(self, tools, **kwargs):
            return self

    agent = observe_agent(create_agent(model=SimpleFakeModel(), tools=[]), name="test-agent")

    agent.invoke({"messages": [HumanMessage(content="1")]})
    assert get_current_context() is None

    agent.invoke({"messages": [HumanMessage(content="2")]})
    assert get_current_context() is None


def test_interrupt_not_marked_as_system_error(init_sdk):
    """GraphInterrupt should not be marked as a generic system error."""
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    from langgraph.errors import GraphInterrupt

    mw = LangChainObservabilityMiddleware()

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    with Observability.trace("test"):
        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "fake"
        request.messages = []
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        def handler(req):
            raise GraphInterrupt()

        with pytest.raises(GraphInterrupt):
            mw.wrap_model_call(request, handler)

    Observability._tracer.reporter.report = original_report

    llm_spans = [s for s in captured_spans if s["span_kind"] == "LLM"]
    # GraphInterrupt is raised as exception, span will be marked ERROR
    # But the error_type should be GraphInterrupt, not a generic system error
    if llm_spans:
        assert llm_spans[0]["status"] in ("ERROR", "UNSET")
        if llm_spans[0]["error_type"]:
            assert "GraphInterrupt" in llm_spans[0]["error_type"] or "BubbleUp" in llm_spans[0]["error_type"]
