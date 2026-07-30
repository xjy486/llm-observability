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


def test_create_agent_invoke_auto_instrumented():
    """create_agent().invoke() produces an AGENT (auto-root) via Pregel patch."""
    _clean_init(auto_instrument_langchain=True)
    try:
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        from langgraph.prebuilt import create_agent

        model = GenericFakeChatModel(messages=iter([AIMessage(content="done")]))
        agent_obj = create_agent(model=model, tools=[], prompt="answer")
        result = agent_obj.invoke({"messages": [{"role": "user", "content": "hi"}]})
        # Drain
        if Observability._initialized:
            Observability.shutdown()
        recs = _records()
        kinds = [r["span_kind"] for r in recs]
        assert "AGENT" in kinds, f"missing AGENT (auto-root) in {kinds}"
    except ImportError:
        pytest.skip("langgraph create_agent not available")
    finally:
        if Observability._initialized:
            Observability.shutdown()


def test_create_agent_ainvoke_auto_instrumented():
    """create_agent().ainvoke() produces an AGENT (auto-root) async."""
    _clean_init(auto_instrument_langchain=True)
    try:
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        from langgraph.prebuilt import create_agent

        model = GenericFakeChatModel(messages=iter([AIMessage(content="done")]))
        agent_obj = create_agent(model=model, tools=[], prompt="answer")

        async def run():
            return await agent_obj.ainvoke({"messages": [{"role": "user", "content": "hi"}]})

        asyncio.run(run())
        if Observability._initialized:
            Observability.shutdown()
        recs = _records()
        kinds = [r["span_kind"] for r in recs]
        assert "AGENT" in kinds, f"missing AGENT (auto-root) in {kinds}"
    except ImportError:
        pytest.skip("langgraph create_agent not available")
    finally:
        if Observability._initialized:
            Observability.shutdown()


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
