"""Regression coverage for Phase 2.4 closeout blockers and P1 behavior."""
import asyncio
import contextvars

import pytest
from langchain_core.callbacks import BaseCallbackHandler, CallbackManager
from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableLambda, RunnableParallel
from langchain_core.tools import tool

from llm_observability import Observability
from llm_observability.context import get_current_context, set_context
from llm_observability.integrations.langchain.callback_handler import (
    LangChainObservabilityCallbackHandler,
)
from llm_observability.integrations.langchain.callback_registry import CallbackRunState
from llm_observability.integrations.langchain.runnable_wrapper import (
    _inject_callback_handler,
    observe_runnable,
)
from llm_observability.span_registry import get_span_event_sink


class FakeChatModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hello"))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hello"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGeneration(message=AIMessage(content="hello"))


@pytest.fixture
def init_sdk():
    Observability.init(
        app_name="closeout-tests",
        endpoint="http://localhost:9999",
        auto_instrument_openai=False,
    )
    Observability._tracer.reporter.report = lambda record: None
    yield
    Observability.shutdown()


def _capture_reports():
    captured = []
    original = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = captured.append
    return captured, original


def _restore_reports(original):
    Observability._tracer.reporter.report = original


def _chat_chain():
    return (
        RunnableLambda(lambda value: [HumanMessage(content=str(value["input"]))])
        | FakeChatModel()
        | StrOutputParser()
    )


def test_callback_manager_model_callback_inherited(init_sdk):
    captured, original = _capture_reports()
    manager = CallbackManager(handlers=[], inheritable_handlers=[])
    observed = observe_runnable(_chat_chain(), name="callback-manager-model")
    try:
        observed.invoke({"input": "hello"}, config={"callbacks": manager})
    finally:
        _restore_reports(original)
    llms = [s for s in captured if s["span_kind"] == "LLM"]
    assert len(llms) == 1
    assert len(manager.handlers) == 0
    assert len(manager.inheritable_handlers) == 0


def test_callback_manager_tool_callback_inherited(init_sdk):
    captured, original = _capture_reports()

    @tool
    def echo_tool(value: str) -> str:
        """Echo a value."""
        return value

    manager = CallbackManager(handlers=[], inheritable_handlers=[])
    handler = LangChainObservabilityCallbackHandler()
    try:
        with Observability.trace(name="callback-manager-tool") as trace:
            handler._root_span = trace._span
            config = _inject_callback_handler({"callbacks": manager}, handler)
            cloned = config["callbacks"]
            assert handler in cloned.inheritable_handlers
            echo_tool.run("hello", callbacks=cloned)
    finally:
        _restore_reports(original)
    tools = [s for s in captured if s["span_kind"] == "TOOL"]
    assert len(tools) == 1
    assert len(manager.handlers) == 0
    assert len(manager.inheritable_handlers) == 0


def test_callback_manager_retriever_callback_inherited(init_sdk):
    class FakeRetriever(BaseRetriever):
        def _get_relevant_documents(self, query, *, run_manager):
            return [Document(page_content=query)]

    captured, original = _capture_reports()
    manager = CallbackManager(handlers=[], inheritable_handlers=[])
    observed = observe_runnable(FakeRetriever(), name="callback-manager-retriever")
    try:
        observed.invoke("hello", config={"callbacks": manager})
    finally:
        _restore_reports(original)
    assert len([
        s for s in captured
        if s["span_kind"] == "TOOL"
        and s.get("attributes", {}).get("langchain.component") == "retriever"
    ]) == 1
    assert len(manager.handlers) == 0
    assert len(manager.inheritable_handlers) == 0


def test_callback_manager_repeated_invokes_no_accumulation(init_sdk):
    captured, original = _capture_reports()
    manager = CallbackManager(handlers=[], inheritable_handlers=[])
    observed = observe_runnable(_chat_chain(), name="callback-manager-repeat")
    try:
        observed.invoke({"input": "one"}, config={"callbacks": manager})
        observed.invoke({"input": "two"}, config={"callbacks": manager})
    finally:
        _restore_reports(original)
    assert len([s for s in captured if s["span_kind"] == "LLM"]) == 2
    assert len(manager.handlers) == 0
    assert len(manager.inheritable_handlers) == 0


def _sink_for(span):
    return get_span_event_sink(span.trace_id, span.span_id)


def test_tool_event_sink_removed_after_end(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="tool-sink-end"):
        handler.on_tool_start({"name": "tool"}, "input", "tool-end")
        state = handler._registry.get("tool-end")
        span = state.span._span
        assert _sink_for(span) is not None
        handler.on_tool_end("output", "tool-end")
        assert _sink_for(span) is None


def test_tool_event_sink_removed_after_error(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="tool-sink-error"):
        handler.on_tool_start({"name": "tool"}, "input", "tool-error")
        span = handler._registry.get("tool-error").span._span
        handler.on_tool_error(RuntimeError("boom"), "tool-error")
        assert _sink_for(span) is None


def test_retriever_event_sink_removed_after_end(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="retriever-sink-end"):
        handler.on_retriever_start({"name": "retriever"}, "query", "retriever-end")
        span = handler._registry.get("retriever-end").span._span
        handler.on_retriever_end([], "retriever-end")
        assert _sink_for(span) is None


def test_llm_event_sink_removed_when_span_end_fails(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="llm-sink-error"):
        handler.on_chat_model_start(
            {"name": "model"}, [[HumanMessage(content="hello")]], "llm-end-fails"
        )
        state = handler._registry.get("llm-end-fails")
        span = state._llm_span

        def fail_end():
            raise RuntimeError("end failed")

        span.end = fail_end
        handler.on_llm_end(None, "llm-end-fails")
        assert _sink_for(span) is None


def test_handler_close_removes_all_global_sinks(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="close-tool-sink"):
        handler.on_tool_start({"name": "tool"}, "input", "open-tool")
        tool_span = handler._registry.get("open-tool").span._span
        handler.close_open_runs(reason="test")
        assert _sink_for(tool_span) is None
    with Observability.trace(name="close-retriever-sink"):
        handler.on_retriever_start({"name": "retriever"}, "query", "open-retriever")
        retriever_span = handler._registry.get("open-retriever").span._span
        handler.close_open_runs(reason="test")
        assert _sink_for(retriever_span) is None


def test_custom_event_name_retained_when_payload_off(init_sdk):
    Observability.shutdown()
    Observability.init(
        app_name="closeout-tests",
        endpoint="http://localhost:9999",
        auto_instrument_openai=False,
        payload_strategy="off",
    )
    Observability._tracer.reporter.report = lambda record: None
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="custom-off"):
        ctx = get_current_context()
        handler._registry.register(
            CallbackRunState(
                run_id="custom-off-run",
                parent_run_id=None,
                run_type="chain",
                name="chain",
                context=ctx,
                span=None,
                token=None,
                context_owner=False,
                virtual=True,
                sampled=True,
                first_token_seen=False,
                started_at=0,
                ended=False,
            )
        )
        handler.on_custom_event(
            "user event/with data",
            {"password": "secret"},
            "custom-off-run",
        )
        root_sink = get_span_event_sink(ctx.trace_id, ctx.span_id)
        assert root_sink is not None
        event = root_sink._span.events[-1]
        assert event["name"].startswith("langchain.custom.user-event-with-data")
        assert "langchain.data" not in event["attributes"]


def test_async_llm_start_end_restores_agent_context(init_sdk):
    async def run():
        handler = LangChainObservabilityCallbackHandler()
        with Observability.trace(name="async-context"):
            agent_context = get_current_context()
            handler.on_chat_model_start(
                {"name": "model"}, [[HumanMessage(content="hello")]], "async-llm"
            )
            stale_context = contextvars.copy_context()
            set_context(agent_context)

            async def finish():
                handler.on_llm_end(None, "async-llm")
                return get_current_context()

            result_context = await asyncio.create_task(stale_context.run(finish))
            assert result_context == agent_context

    asyncio.run(run())


def test_runnable_parallel_branches_do_not_pollute_context(init_sdk):
    captured, original = _capture_reports()
    observed = observe_runnable(
        RunnableParallel(left=_chat_chain(), right=_chat_chain()),
        name="parallel-context",
    )
    try:
        observed.invoke({"input": "hello"})
    finally:
        _restore_reports(original)
    assert get_current_context() is None
    llms = [s for s in captured if s["span_kind"] == "LLM"]
    assert len(llms) == 2
    assert len({s["parent_span_id"] for s in llms}) == 1


@pytest.mark.asyncio
async def test_consecutive_ainvoke_does_not_share_llm_context(init_sdk):
    observed = observe_runnable(_chat_chain(), name="async-repeat")
    await observed.ainvoke({"input": "one"})
    assert get_current_context() is None
    await observed.ainvoke({"input": "two"})
    assert get_current_context() is None


@pytest.mark.asyncio
async def test_astream_early_aclose_restores_context(init_sdk):
    observed = observe_runnable(_chat_chain(), name="async-stream-close")
    stream = observed.astream({"input": "hello"})
    await stream.__anext__()
    await stream.aclose()
    assert get_current_context() is None


def test_tool_local_span_reference_removed_after_end(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="tool-local-end"):
        handler.on_tool_start({"name": "tool"}, "input", "tool-local-end")
        span = handler._registry.get("tool-local-end").span._span
        assert str(span.span_id) in handler._spans_by_id
        handler.on_tool_end("output", "tool-local-end")
        assert str(span.span_id) not in handler._spans_by_id
        assert _sink_for(span) is None


def test_retriever_local_span_reference_removed_after_end(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="retriever-local-end"):
        handler.on_retriever_start({"name": "retriever"}, "query", "retriever-local-end")
        span = handler._registry.get("retriever-local-end").span._span
        handler.on_retriever_end([], "retriever-local-end")
        assert str(span.span_id) not in handler._spans_by_id
        assert _sink_for(span) is None


def test_llm_local_span_reference_removed_after_end(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="llm-local-end"):
        handler.on_chat_model_start(
            {"name": "model"}, [[HumanMessage(content="hello")]], "llm-local-end"
        )
        span = handler._registry.get("llm-local-end")._llm_span
        handler.on_llm_end(None, "llm-local-end")
        assert str(span.span_id) not in handler._spans_by_id
        assert _sink_for(span) is None


def test_manual_callback_handler_reuse_has_bounded_span_map(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    for i in range(3):
        run_id = f"reuse-llm-{i}"
        with Observability.trace(name=f"reuse-trace-{i}"):
            handler.on_chat_model_start(
                {"name": "model"}, [[HumanMessage(content="hello")]], run_id
            )
            handler.on_llm_end(None, run_id)
    assert len(handler._spans_by_id) == 0


def test_span_end_failure_removes_local_and_global_reference(init_sdk):
    handler = LangChainObservabilityCallbackHandler()
    with Observability.trace(name="llm-end-failure-local"):
        handler.on_chat_model_start(
            {"name": "model"}, [[HumanMessage(content="hello")]], "llm-end-failure-local"
        )
        state = handler._registry.get("llm-end-failure-local")
        span = state._llm_span

        def fail_end():
            raise RuntimeError("end failed")

        span.end = fail_end
        handler.on_llm_end(None, "llm-end-failure-local")
        assert str(span.span_id) not in handler._spans_by_id
        assert _sink_for(span) is None
