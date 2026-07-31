"""Phase 2.5 final closeout — Blocker 1/2/3 tests.

Blocker 1: create_agent / CompiledGraph (Pregel) auto-instrumentation
Blocker 2: user callback object identity preserved + CallbackManager semantics
Blocker 3: generator/async-generator init fail-open
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest

from llm_observability import Observability
from llm_observability.decorators import agent, chain, tool
from llm_observability.context import get_current_context


def _clean_init(**kwargs):
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(
        app_name="t", endpoint="http://localhost:99999",
        auto_instrument_openai=False, **kwargs,
    )
    return Observability._tracer


def _records():
    return list(Observability._tracer.reporter._queue)


# ── Blocker 1: create_agent / Pregel auto-instrumentation ──

def test_pregel_is_collected_as_candidate():
    """Pregel is in the candidate classes so create_agent.invoke is patched."""
    from llm_observability.instrumentation.langchain import _collect_candidate_classes
    candidates = _collect_candidate_classes()
    names = [c.__name__ for c in candidates]
    assert "Pregel" in names, f"Pregel not in candidates: {names}"


# Minimal fake chat model that supports bind_tools (required by create_agent).
# GenericFakeChatModel.bind_tools raises NotImplementedError.
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class _FakeToolCallingModel(BaseChatModel):
    """Minimal fake chat model supporting bind_tools for create_agent tests."""
    response: str = "done"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(content=self.response)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self):
        return "fake-tool-calling"


def _build_agent():
    """Create a create_agent instance with a fake model (no real API needed)."""
    from langchain.agents import create_agent
    model = _FakeToolCallingModel()
    return create_agent(model=model, tools=[], system_prompt="answer")


def test_create_agent_invoke_auto_instrumented():
    """create_agent().invoke() produces AGENT + LLM via Pregel patch."""
    _clean_init(auto_instrument_langchain=True)
    agent_obj = _build_agent()
    result = agent_obj.invoke({"messages": [{"role": "user", "content": "hi"}]})
    assert result is not None
    recs = _records()
    Observability.shutdown()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(agent_recs) == 1, f"expected 1 AGENT, got {len(agent_recs)}"
    assert len(llm_recs) >= 1, f"expected >=1 LLM, got {len(llm_recs)}"
    # No duplicate LLM
    assert len(llm_recs) == 1, f"expected exactly 1 LLM (no dup), got {len(llm_recs)}"
    # All spans share the same trace
    trace_ids = {r["trace_id"] for r in recs}
    assert len(trace_ids) == 1, f"expected 1 trace_id, got {trace_ids}"


def test_create_agent_ainvoke_auto_instrumented():
    """create_agent().ainvoke() produces AGENT + LLM async."""
    _clean_init(auto_instrument_langchain=True)
    agent_obj = _build_agent()

    async def run():
        return await agent_obj.ainvoke({"messages": [{"role": "user", "content": "hi"}]})

    result = asyncio.run(run())
    assert result is not None
    recs = _records()
    Observability.shutdown()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(agent_recs) == 1, f"expected 1 AGENT, got {len(agent_recs)}"
    assert len(llm_recs) == 1, f"expected exactly 1 LLM (no dup), got {len(llm_recs)}"
    trace_ids = {r["trace_id"] for r in recs}
    assert len(trace_ids) == 1, f"expected 1 trace_id, got {trace_ids}"


def test_create_agent_stream_auto_instrumented():
    """create_agent().stream() produces AGENT + LLM via Pregel patch."""
    _clean_init(auto_instrument_langchain=True)
    agent_obj = _build_agent()
    chunks = list(agent_obj.stream({"messages": [{"role": "user", "content": "hi"}]}))
    assert chunks, "expected at least one stream chunk"
    recs = _records()
    Observability.shutdown()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(agent_recs) == 1, f"expected 1 AGENT, got {len(agent_recs)}"
    assert len(llm_recs) >= 1, f"expected >=1 LLM, got {len(llm_recs)}"
    trace_ids = {r["trace_id"] for r in recs}
    assert len(trace_ids) == 1, f"expected 1 trace_id, got {trace_ids}"


def test_create_agent_astream_auto_instrumented():
    """create_agent().astream() produces AGENT + LLM async."""
    _clean_init(auto_instrument_langchain=True)
    agent_obj = _build_agent()

    async def run():
        chunks = []
        async for chunk in agent_obj.astream({"messages": [{"role": "user", "content": "hi"}]}):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    assert chunks, "expected at least one stream chunk"
    recs = _records()
    Observability.shutdown()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(agent_recs) == 1, f"expected 1 AGENT, got {len(agent_recs)}"
    assert len(llm_recs) >= 1, f"expected >=1 LLM, got {len(llm_recs)}"
    trace_ids = {r["trace_id"] for r in recs}
    assert len(trace_ids) == 1, f"expected 1 trace_id, got {trace_ids}"


# ── Blocker 2: user callback identity + CallbackManager ──

def test_user_callback_object_identity_preserved():
    """The original user callback handler instance is called (not a deepcopy)."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler

    class StatefulHandler(BaseCallbackHandler):
        def __init__(self):
            self.chain_start_count = 0
            self.identity = id(self)

        def on_chain_start(self, serialized, inputs, **kwargs):
            self.chain_start_count += 1

    handler = StatefulHandler()
    chain = RunnableLambda(lambda x: x)
    chain.invoke("hi", config={"callbacks": [handler]})
    if Observability._initialized:
        Observability.shutdown()
    # The ORIGINAL handler instance was called (not a clone)
    assert handler.chain_start_count >= 1, "original handler was not called"


def test_stateful_user_callback_instance_is_called():
    """A stateful callback's counter increments on the original instance."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler

    class Counter(BaseCallbackHandler):
        def __init__(self):
            self.count = 0
        def on_chain_start(self, *a, **kw):
            self.count += 1

    h = Counter()
    chain = RunnableLambda(lambda x: x) | RunnableLambda(lambda x: x)
    chain.invoke("hi", config={"callbacks": [h]})
    if Observability._initialized:
        Observability.shutdown()
    assert h.count >= 2, f"expected >=2 chain_start on original instance, got {h.count}"


def test_callback_manager_metadata_preserved():
    """CallbackManager metadata/tags are preserved (not stripped to .handlers)."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackManager, BaseCallbackHandler

    class Noop(BaseCallbackHandler):
        def on_chain_start(self, *a, **kw): pass

    from langchain_core.callbacks.manager import CallbackManager
    mgr = CallbackManager(handlers=[Noop()], tags=["my-tag"], metadata={"k": "v"})
    chain = RunnableLambda(lambda x: x)
    # Should not raise; the manager is cloned with full semantics
    chain.invoke("hi", config={"callbacks": mgr})
    if Observability._initialized:
        Observability.shutdown()
    # Original manager unchanged
    assert "my-tag" in mgr.tags
    assert mgr.metadata.get("k") == "v"


def test_callback_manager_tags_preserved():
    """CallbackManager tags survive the clone."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import CallbackManager

    class Noop(BaseCallbackHandler):
        def on_chain_start(self, *a, **kw): pass

    mgr = CallbackManager(handlers=[Noop()], tags=["t1", "t2"])
    chain = RunnableLambda(lambda x: x)
    chain.invoke("hi", config={"callbacks": mgr})
    if Observability._initialized:
        Observability.shutdown()
    assert mgr.tags == ["t1", "t2"]


def test_callback_manager_inheritable_handlers_preserved():
    """CallbackManager inheritable_handlers survive the clone (Blocker 2)."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import CallbackManager

    class InheritableNoop(BaseCallbackHandler):
        def on_chain_start(self, *a, **kw): pass

    inheritable_h = InheritableNoop()
    mgr = CallbackManager(
        handlers=[],
        inheritable_handlers=[inheritable_h],
        tags=["t1"],
        inheritable_tags=["it1"],
        metadata={"k": "v"},
        inheritable_metadata={"ik": "iv"},
    )
    chain = RunnableLambda(lambda x: x)
    chain.invoke("hi", config={"callbacks": mgr})
    if Observability._initialized:
        Observability.shutdown()
    # Original manager's inheritable_handlers not mutated
    assert inheritable_h in mgr.inheritable_handlers
    assert mgr.inheritable_tags == ["it1"]
    assert mgr.inheritable_metadata.get("ik") == "iv"


# ── Blocker 1: CallbackManager observability handler inherited by child runs ──

def test_callback_manager_observability_inherited_by_child_model():
    """Observability handler is inherited by child model runs via inherit=True.

    When a user passes a CallbackManager, the auto wrapper clones it and adds
    the Observability handler with inherit=True. Child model runs should
    receive the handler and create LLM spans.

    The user handler (non-inheritable) is called for the root run.
    The Observability handler (inheritable) is inherited to child model runs.
    """
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import CallbackManager

    user_events = {"chain_start": 0}

    class UserRootHandler(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, **kwargs):
            user_events["chain_start"] += 1

    mgr = CallbackManager(handlers=[UserRootHandler()], tags=["utag"])
    # Chain: model is a child run of the sequence
    chain = _FakeToolCallingModel() | RunnableLambda(lambda x: str(x))
    chain.invoke("hi", config={"callbacks": mgr})
    recs = _records()
    Observability.shutdown()
    # User handler called for root run (preserved in cloned manager)
    assert user_events["chain_start"] >= 1, "user handler not called for root run"
    # LLM span created (Observability handler inherited to child model)
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(llm_recs) == 1, f"expected 1 LLM (handler inherited to child model), got {len(llm_recs)}"
    # Original manager not mutated
    assert len(mgr.handlers) == 1, "original manager handlers mutated"
    assert "utag" in mgr.tags


def test_callback_manager_observability_inherited_by_child_tool():
    """Observability handler is inherited by child tool runs via inherit=True."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import CallbackManager
    from langchain_core.tools import tool as langchain_tool

    user_events = {"chain_start": 0}

    class UserRootHandler(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, **kwargs):
            user_events["chain_start"] += 1

    @langchain_tool
    def search(query: str) -> str:
        """Search."""
        return f"result:{query}"

    mgr = CallbackManager(handlers=[UserRootHandler()], tags=["utag"])
    chain = search | RunnableLambda(lambda x: str(x))
    chain.invoke("hello", config={"callbacks": mgr})
    recs = _records()
    Observability.shutdown()
    # User handler called for root run
    assert user_events["chain_start"] >= 1, "user handler not called for root run"
    # TOOL span created (Observability handler inherited to child tool)
    tool_recs = [r for r in recs if r["span_kind"] == "TOOL"]
    assert len(tool_recs) >= 1, f"expected >=1 TOOL (handler inherited to child tool), got {len(tool_recs)}"
    # Original manager not mutated
    assert len(mgr.handlers) == 1
    assert "utag" in mgr.tags


def test_callback_manager_observability_inherited_by_retriever():
    """Observability handler is inherited by child retriever runs."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import CallbackManager
    from langchain_core.retrievers import BaseRetriever
    from langchain_core.documents import Document

    user_events = {"chain_start": 0}

    class UserRootHandler(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, **kwargs):
            user_events["chain_start"] += 1

    class FakeRetriever(BaseRetriever):
        def _get_relevant_documents(self, query, *, run_manager):
            return [Document(page_content="doc1"), Document(page_content="doc2")]

    mgr = CallbackManager(handlers=[UserRootHandler()], tags=["utag"])
    # Retriever as child run of a sequence
    retriever = FakeRetriever()
    chain = retriever | RunnableLambda(lambda docs: str(len(docs)))
    chain.invoke("query", config={"callbacks": mgr})
    recs = _records()
    Observability.shutdown()
    # User handler called for root run
    assert user_events["chain_start"] >= 1, "user handler not called for root run"
    # Original manager not mutated
    assert len(mgr.handlers) == 1
    assert "utag" in mgr.tags


def test_async_callback_manager_observability_inherited():
    """AsyncCallbackManager: Observability handler inherited by child model runs."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import AsyncCallbackManager

    user_events = {"chain_start": 0}

    class UserRootAsyncHandler(BaseCallbackHandler):
        async def on_chain_start(self, serialized, inputs, **kwargs):
            user_events["chain_start"] += 1

    mgr = AsyncCallbackManager(handlers=[UserRootAsyncHandler()], tags=["atag"])

    async def echo(x):
        return str(x)

    chain = _FakeToolCallingModel() | RunnableLambda(echo)

    async def run():
        return await chain.ainvoke("hi", config={"callbacks": mgr})

    result = asyncio.run(run())
    assert result is not None
    recs = _records()
    Observability.shutdown()
    # User handler called for root run
    assert user_events["chain_start"] >= 1, "async user handler not called for root run"
    # LLM span created (Observability handler inherited to child model)
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(llm_recs) == 1, f"expected 1 LLM (handler inherited to child model), got {len(llm_recs)}"
    # Original manager not mutated
    assert len(mgr.handlers) == 1
    assert "atag" in mgr.tags


# ── Blocker 3: generator init fail-open ──

def test_task_generator_set_context_failure_fail_open():
    """TASK sync generator: set_context failure + fail_open runs business."""
    _clean_init(fail_open=True)
    tracer = Observability._tracer
    import llm_observability.task as task_mod
    original_set = task_mod.set_context
    def failing_set(ctx):
        raise RuntimeError("set_context boom")
    task_mod.set_context = failing_set
    try:
        from llm_observability.decorators import chain
        @chain()
        def gen():
            yield "a"
            yield "b"
        with tracer.trace(name="root"):
            result = list(gen())
        assert result == ["a", "b"]
    finally:
        task_mod.set_context = original_set
    Observability.shutdown()


def test_task_async_generator_set_context_failure_fail_open():
    """TASK async generator: set_context failure + fail_open runs business."""
    _clean_init(fail_open=True)
    tracer = Observability._tracer
    import llm_observability.task as task_mod
    original_set = task_mod.set_context
    def failing_set(ctx):
        raise RuntimeError("set_context boom")
    task_mod.set_context = failing_set
    try:
        from llm_observability.decorators import chain
        @chain()
        async def agen():
            yield "a"
            yield "b"
        async def run():
            with tracer.trace(name="root"):
                return [x async for x in agen()]
        result = asyncio.run(run())
        assert result == ["a", "b"]
    finally:
        task_mod.set_context = original_set
    Observability.shutdown()


def test_tool_generator_set_context_failure_fail_open():
    """TOOL sync generator: set_context failure + fail_open runs business."""
    _clean_init(fail_open=True)
    tracer = Observability._tracer
    import llm_observability.tool as tool_mod
    original_set = tool_mod.set_context
    def failing_set(ctx):
        raise RuntimeError("set_context boom")
    tool_mod.set_context = failing_set
    try:
        from llm_observability.decorators import tool
        @tool()
        def gen():
            yield "a"
        with tracer.trace(name="root"):
            result = list(gen())
        assert result == ["a"]
    finally:
        tool_mod.set_context = original_set
    Observability.shutdown()


def test_tool_async_generator_set_context_failure_fail_open():
    """TOOL async generator: set_context failure + fail_open runs business."""
    _clean_init(fail_open=True)
    tracer = Observability._tracer
    import llm_observability.tool as tool_mod
    original_set = tool_mod.set_context
    def failing_set(ctx):
        raise RuntimeError("set_context boom")
    tool_mod.set_context = failing_set
    try:
        from llm_observability.decorators import tool
        @tool()
        async def agen():
            yield "a"
        async def run():
            with tracer.trace(name="root"):
                return [x async for x in agen()]
        result = asyncio.run(run())
        assert result == ["a"]
    finally:
        tool_mod.set_context = original_set
    Observability.shutdown()


def test_generator_init_failure_fail_closed():
    """Generator span init failure + fail_open=False propagates."""
    _clean_init(fail_open=False)
    tracer = Observability._tracer
    import llm_observability.task as task_mod
    original_set = task_mod.set_context
    def failing_set(ctx):
        raise RuntimeError("set_context boom")
    task_mod.set_context = failing_set
    try:
        from llm_observability.decorators import chain
        @chain()
        def gen():
            yield "a"
        with tracer.trace(name="root"):
            with pytest.raises(RuntimeError):
                list(gen())
    finally:
        task_mod.set_context = original_set
    Observability.shutdown()
