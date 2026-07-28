"""Phase 2.4: Runnable Wrapper tests.

Tests observe_runnable() with real LCEL chains using FakeChatModel.
Verifies: AGENT trace creation, LLM span, no new SpanKinds, callback preservation.
"""
import pytest
from typing import ClassVar
from llm_observability import Observability
from llm_observability.integrations.langchain.runnable_wrapper import observe_runnable
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser


class FakeChatModel(BaseChatModel):
    """Simple fake model for testing."""
    @property
    def _llm_type(self):
        return "fake"
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hello world"))])
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hello world"))])


@pytest.fixture
def init_sdk():
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    yield
    Observability.shutdown()


def test_observe_runnable_returns_wrapper(init_sdk):
    chain = ChatPromptTemplate.from_messages([("human", "hi")]) | FakeChatModel() | StrOutputParser()
    observed = observe_runnable(chain, name="test-chain")
    assert observed is not None
    assert hasattr(observed, "invoke")
    assert hasattr(observed, "ainvoke")
    assert hasattr(observed, "stream")
    assert hasattr(observed, "astream")


def test_invalid_root_mode_raises():
    with pytest.raises(ValueError, match="root_mode"):
        observe_runnable(object(), root_mode="invalid")


def test_non_runnable_raises():
    with pytest.raises(ValueError, match="invoke"):
        observe_runnable(object())


def test_invoke_creates_agent_trace(init_sdk):
    captured = []
    orig = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured.append(r)

    chain = ChatPromptTemplate.from_messages([("human", "{input}")]) | FakeChatModel() | StrOutputParser()
    observed = observe_runnable(chain, name="test-chain")
    result = observed.invoke({"input": "hello"})

    Observability._tracer.reporter.report = orig
    assert "hello world" in result

    agents = [s for s in captured if s["span_kind"] == "AGENT"]
    assert len(agents) == 1, f"Expected 1 AGENT, got {len(agents)}"
    assert "runnable.test-chain" in agents[0]["span_name"]

    llms = [s for s in captured if s["span_kind"] == "LLM"]
    assert len(llms) == 1, f"Expected 1 LLM, got {len(llms)}"

    # LLM parent should be AGENT
    agent_span_id = agents[0]["span_id"]
    assert llms[0]["parent_span_id"] == agent_span_id


def test_no_new_spankinds(init_sdk):
    """Ensure no CHAIN/RUNNABLE/PROMPT/PARSER span kinds."""
    captured = []
    orig = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured.append(r)

    chain = ChatPromptTemplate.from_messages([("human", "hi")]) | FakeChatModel() | StrOutputParser()
    observed = observe_runnable(chain, name="test-chain")
    observed.invoke({"input": "hello"})

    Observability._tracer.reporter.report = orig
    kinds = {s["span_kind"] for s in captured}
    forbidden = {"CHAIN", "RUNNABLE", "PROMPT", "PARSER"}
    assert not (kinds & forbidden), f"Forbidden span kinds found: {kinds & forbidden}"


def test_root_attributes_set(init_sdk):
    captured = []
    orig = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured.append(r)

    chain = ChatPromptTemplate.from_messages([("human", "hi")]) | FakeChatModel() | StrOutputParser()
    observed = observe_runnable(chain, name="test-chain")
    observed.invoke({"input": "hello"})

    Observability._tracer.reporter.report = orig
    agents = [s for s in captured if s["span_kind"] == "AGENT"]
    assert len(agents) == 1
    attrs = agents[0]["attributes"]
    assert attrs.get("framework.name") == "langchain"
    assert attrs.get("langchain.component") == "runnable"
    assert attrs.get("langchain.runnable.name") == "test-chain"


def test_llm_callback_attributes(init_sdk):
    captured = []
    orig = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured.append(r)

    chain = ChatPromptTemplate.from_messages([("human", "hi")]) | FakeChatModel() | StrOutputParser()
    observed = observe_runnable(chain, name="test-chain")
    observed.invoke({"input": "hello"})

    Observability._tracer.reporter.report = orig
    llms = [s for s in captured if s["span_kind"] == "LLM"]
    assert len(llms) == 1
    attrs = llms[0]["attributes"]
    assert attrs.get("langchain.callback.mode") == "true"
    assert attrs.get("langchain.component") == "model"


def test_user_callbacks_preserved(init_sdk):
    """User callbacks must be preserved alongside observability handler."""
    from langchain_core.callbacks import BaseCallbackHandler
    user_calls = []

    class UserHandler(BaseCallbackHandler):
        def on_chain_start(self, *args, **kwargs):
            user_calls.append("chain_start")
        def on_chain_end(self, *args, **kwargs):
            user_calls.append("chain_end")

    chain = ChatPromptTemplate.from_messages([("human", "hi")]) | FakeChatModel() | StrOutputParser()
    observed = observe_runnable(chain, name="test-chain")
    result = observed.invoke({"input": "hello"}, config={"callbacks": [UserHandler()]})
    assert len(user_calls) > 0, "User callbacks should have been called"


def test_handler_noop_without_trace():
    """No Active Trace → Callback Handler No-op."""
    from llm_observability.integrations.langchain.callback_handler import LangChainObservabilityCallbackHandler
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    try:
        handler = LangChainObservabilityCallbackHandler()
        # Should not raise
        handler.on_chain_start(serialized={}, inputs={}, run_id="r1")
        handler.on_chain_end(outputs={}, run_id="r1")
        handler.on_chat_model_start(serialized={}, messages=[], run_id="r2")
        handler.on_llm_end(response=None, run_id="r2")
    finally:
        Observability.shutdown()


def test_chain_events_recorded(init_sdk):
    """Chain events should be recorded on the AGENT span."""
    captured = []
    orig = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured.append(r)

    chain = ChatPromptTemplate.from_messages([("human", "hi")]) | FakeChatModel() | StrOutputParser()
    observed = observe_runnable(chain, name="test-chain")
    observed.invoke({"input": "hello"})

    Observability._tracer.reporter.report = orig
    agents = [s for s in captured if s["span_kind"] == "AGENT"]
    assert len(agents) == 1
    events = agents[0]["events"]
    chain_events = [e for e in events if "langchain.chain" in e.get("name", "")]
    assert len(chain_events) >= 2, f"Expected at least 2 chain events, got {len(chain_events)}"


def test_ainvoke_creates_agent_trace(init_sdk):
    import asyncio
    captured = []
    orig = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured.append(r)

    chain = ChatPromptTemplate.from_messages([("human", "{input}")]) | FakeChatModel() | StrOutputParser()
    observed = observe_runnable(chain, name="test-chain")
    result = asyncio.run(observed.ainvoke({"input": "hello"}))

    Observability._tracer.reporter.report = orig
    assert "hello world" in result
    agents = [s for s in captured if s["span_kind"] == "AGENT"]
    assert len(agents) == 1
    llms = [s for s in captured if s["span_kind"] == "LLM"]
    assert len(llms) == 1
