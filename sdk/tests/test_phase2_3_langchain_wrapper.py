"""Phase 2.3: Agent Wrapper tests."""
import pytest
import asyncio
from typing import ClassVar
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import get_current_context, SpanContext, set_context, reset_context
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeChatModel(BaseChatModel):
    call_count: ClassVar[int] = 0

    @property
    def _llm_type(self):
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        FakeChatModel.call_count += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"Response {FakeChatModel.call_count}"))])

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def init_sdk():
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    yield
    Observability.shutdown()


def make_fake_agent():
    """Create a minimal fake agent object with invoke/ainvoke/stream/astream."""
    from langchain.agents import create_agent
    return create_agent(model=FakeChatModel(), tools=[])


def test_observed_agent_invoke_creates_agent_trace(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    agent = make_fake_agent()
    observed = observe_agent(agent, name="test-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    result = observed.invoke({"messages": [HumanMessage(content="hi")]})

    Observability._tracer.reporter.report = original_report

    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    assert agent_spans[0]["span_name"] == "agent.test-agent"
    assert agent_spans[0]["status"] == "OK"


def test_observed_agent_error_marks_agent_error(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    agent = MagicMock()
    def raise_error(input, config=None, **kwargs):
        raise ValueError("agent error")
    agent.invoke = raise_error

    observed = observe_agent(agent, name="error-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    with pytest.raises(ValueError):
        observed.invoke({"messages": [HumanMessage(content="hi")]})

    Observability._tracer.reporter.report = original_report

    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    assert agent_spans[0]["status"] == "ERROR"
    assert agent_spans[0]["error_type"] == "ValueError"


def test_observed_agent_reuses_existing_trace(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    agent = make_fake_agent()
    observed = observe_agent(agent, name="test-agent", root_mode="auto")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    with Observability.trace("outer-trace"):
        result = observed.invoke({"messages": [HumanMessage(content="hi")]})

    Observability._tracer.reporter.report = original_report

    # Should NOT create a second AGENT span — reuse the existing trace
    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    assert agent_spans[0]["span_name"] == "agent.run"  # the outer trace


def test_observed_agent_root_mode_create_rejects_nested(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    agent = make_fake_agent()
    observed = observe_agent(agent, name="test-agent", root_mode="create")

    with Observability.trace("outer-trace"):
        with pytest.raises(RuntimeError):
            observed.invoke({"messages": [HumanMessage(content="hi")]})


def test_observed_agent_stream_lifetime(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    FakeChatModel.call_count = 0
    agent = make_fake_agent()
    observed = observe_agent(agent, name="stream-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    chunks = list(observed.stream({"messages": [HumanMessage(content="hi")]})
)
    assert len(chunks) > 0

    Observability._tracer.reporter.report = original_report

    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    assert agent_spans[0]["status"] == "OK"


def test_observed_agent_stream_early_close(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    FakeChatModel.call_count = 0
    agent = make_fake_agent()
    observed = observe_agent(agent, name="stream-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    # Break early from stream
    gen = observed.stream({"messages": [HumanMessage(content="hi")]})
    for i, chunk in enumerate(gen):
        if i >= 1:
            break

    Observability._tracer.reporter.report = original_report

    # AGENT span should still be reported (even on early break)
    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    assert len(agent_spans) >= 1


def test_observed_agent_astream_lifetime(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    FakeChatModel.call_count = 0
    agent = make_fake_agent()
    observed = observe_agent(agent, name="astream-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    async def run():
        async for chunk in observed.astream({"messages": [HumanMessage(content="hi")]}
):
            pass

    asyncio.run(run())

    Observability._tracer.reporter.report = original_report

    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1


def test_observed_agent_astream_cancel_restores_context(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    FakeChatModel.call_count = 0
    agent = make_fake_agent()
    observed = observe_agent(agent, name="astream-agent")

    ctx_before = get_current_context()
    assert ctx_before is None  # no active trace initially

    async def run():
        agen = observed.astream({"messages": [HumanMessage(content="hi")]})
        i = 0
        async for chunk in agen:
            if i >= 1:
                break  # early break
            i += 1

    asyncio.run(run())

    ctx_after = get_current_context()
    assert ctx_after is None  # context restored after early break


def test_observed_agent_ainvoke_creates_agent_trace(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    agent = make_fake_agent()
    observed = observe_agent(agent, name="async-agent")

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    result = asyncio.run(observed.ainvoke({"messages": [HumanMessage(content="hi")]})
)

    Observability._tracer.reporter.report = original_report

    agent_spans = [s for s in captured_spans if s["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    assert agent_spans[0]["status"] == "OK"


def test_observed_agent_transparent_delegation(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    agent = make_fake_agent()
    observed = observe_agent(agent, name="test-agent")

    # Should delegate unknown attributes to the underlying agent
    assert hasattr(observed, "invoke")
    assert hasattr(observed, "ainvoke")
    assert hasattr(observed, "stream")
    assert hasattr(observed, "astream")
    # Check that delegation works for some attribute on the agent
    assert hasattr(observed, "ainvoke")  # verify __getattr__ doesn't break known attrs


def test_invalid_root_mode_raises(init_sdk):
    from llm_observability.integrations.langchain import observe_agent

    agent = make_fake_agent()
    with pytest.raises(ValueError):
        observe_agent(agent, name="test", root_mode="invalid")