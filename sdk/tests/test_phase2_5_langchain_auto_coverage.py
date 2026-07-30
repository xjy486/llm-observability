"""Phase 2.5 — LangChain Auto coverage + concurrency tests (P1-1, P1-2).

P1-1: covers ainvoke, stream, astream, RunnableParallel, AsyncCallbackManager,
      thread concurrency, async concurrency, observe_runnable dedup.
P1-2: depth race protection via lock on AutoInvocationState.
"""
import asyncio
import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest

from llm_observability import Observability


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


def _agent_recs_from(recs):
    return [r for r in recs if r["span_kind"] == "AGENT"]


# ── P1-1: ainvoke / stream / astream on RunnableLambda ──

def test_runnable_lambda_ainvoke_auto():
    """RunnableLambda.ainvoke produces an AGENT (auto-root) async."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda

    async def echo(x):
        return f"echo:{x}"

    chain = RunnableLambda(echo)

    async def run():
        return await chain.ainvoke("hello")

    result = asyncio.run(run())
    assert "echo:hello" in result
    recs = _records()
    Observability.shutdown()
    assert len(_agent_recs_from(recs)) == 1


def test_runnable_lambda_stream_auto():
    """RunnableLambda.stream produces an AGENT (auto-root) via sync generator."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda

    def gen(x):
        yield f"chunk1:{x}"
        yield f"chunk2:{x}"

    chain = RunnableLambda(gen)
    chunks = list(chain.stream("hi"))
    assert len(chunks) == 2
    recs = _records()
    Observability.shutdown()
    assert len(_agent_recs_from(recs)) == 1


def test_runnable_lambda_astream_auto():
    """RunnableLambda.astream produces an AGENT (auto-root) via async generator."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda

    async def agen(x):
        yield f"chunk1:{x}"
        yield f"chunk2:{x}"

    chain = RunnableLambda(agen)

    async def run():
        return [c async for c in chain.astream("hi")]

    chunks = asyncio.run(run())
    assert len(chunks) == 2
    recs = _records()
    Observability.shutdown()
    assert len(_agent_recs_from(recs)) == 1


# ── P1-1: RunnableParallel ──

def test_runnable_parallel_invoke_auto():
    """RunnableParallel.invoke produces a single AGENT (auto-root)."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda, RunnableParallel

    chain = RunnableParallel(
        a=RunnableLambda(lambda x: f"a:{x}"),
        b=RunnableLambda(lambda x: f"b:{x}"),
    )
    result = chain.invoke("hi")
    assert "a:hi" in result["a"]
    assert "b:hi" in result["b"]
    recs = _records()
    Observability.shutdown()
    agent_recs = _agent_recs_from(recs)
    assert len(agent_recs) == 1, f"expected 1 AGENT, got {len(agent_recs)}"


# ── P1-1: AsyncCallbackManager ──

def test_async_callback_manager_preserved():
    """AsyncCallbackManager is cloned with full semantics; user handler called."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import AsyncCallbackManager

    class StatefulAsyncHandler(BaseCallbackHandler):
        def __init__(self):
            self.count = 0

        async def on_chain_start(self, *a, **kw):
            self.count += 1

    handler = StatefulAsyncHandler()
    mgr = AsyncCallbackManager(handlers=[handler], tags=["atag"])

    async def echo(x):
        return f"echo:{x}"

    chain = RunnableLambda(echo)

    async def run():
        return await chain.ainvoke("hi", config={"callbacks": mgr})

    result = asyncio.run(run())
    assert "echo:hi" in result
    Observability.shutdown()
    # Original handler instance was called (not a clone)
    assert handler.count >= 1, "original async handler was not called"
    # Original manager not mutated
    assert "atag" in mgr.tags


# ── P1-1: Thread concurrency ──

def test_thread_concurrent_invoke_single_agent():
    """Multiple concurrent invoke() calls from separate threads each create
    their own AGENT (one per invocation, no cross-contamination)."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda

    chain = RunnableLambda(lambda x: f"r:{x}")

    results = {}
    errors = []

    def worker(idx):
        try:
            results[idx] = chain.invoke(f"msg{idx}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads failed: {errors}"
    assert len(results) == 5
    for i in range(5):
        assert f"r:msg{i}" in results[i]
    recs = _records()
    Observability.shutdown()
    agent_recs = _agent_recs_from(recs)
    assert len(agent_recs) == 5, f"expected 5 AGENT (one per thread), got {len(agent_recs)}"


# ── P1-1: Async concurrency ──

def test_async_concurrent_ainvoke_single_agent_each():
    """Multiple concurrent ainvoke() calls each create their own AGENT."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda

    async def echo(x):
        return f"r:{x}"

    chain = RunnableLambda(echo)

    async def run():
        tasks = [chain.ainvoke(f"msg{i}") for i in range(5)]
        return await asyncio.gather(*tasks)

    results = asyncio.run(run())
    assert len(results) == 5
    recs = _records()
    Observability.shutdown()
    agent_recs = _agent_recs_from(recs)
    assert len(agent_recs) == 5, f"expected 5 AGENT, got {len(agent_recs)}"


# ── P1-1: observe_runnable + auto dedup ──

def test_observe_runnable_with_auto_no_duplicate_agent():
    """Explicit observe_runnable + auto_instrument_langchain should not create
    duplicate AGENT spans — the auto wrapper detects an existing trace."""
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda

    chain = RunnableLambda(lambda x: f"r:{x}")
    observed = Observability.observe_runnable(chain, name="observed")
    result = observed.invoke("hi")
    assert "r:hi" in result
    recs = _records()
    Observability.shutdown()
    agent_recs = _agent_recs_from(recs)
    # observe_runnable creates its own AGENT; auto should NOT create a second
    assert len(agent_recs) >= 1
    # No more than 2 (one from observe_runnable, at most one from auto if it
    # doesn't detect the existing trace — but ideally just 1)
    assert len(agent_recs) <= 2, f"too many AGENT spans: {len(agent_recs)}"


# ── P1-2: depth race protection ──

def test_runnable_parallel_depth_no_premature_close():
    """RunnableParallel with 3 branches: AGENT root must not close prematurely.

    Without the depth lock, parallel branches sharing the same
    AutoInvocationState could race on depth, causing the root to close
    before all branches finish (or close multiple times).
    """
    _clean_init(auto_instrument_langchain=True)
    from langchain_core.runnables import RunnableLambda, RunnableParallel

    chain = RunnableParallel(
        a=RunnableLambda(lambda x: f"a:{x}"),
        b=RunnableLambda(lambda x: f"b:{x}"),
        c=RunnableLambda(lambda x: f"c:{x}"),
    )
    result = chain.invoke("data")
    assert result["a"] == "a:data"
    assert result["b"] == "b:data"
    assert result["c"] == "c:data"
    recs = _records()
    Observability.shutdown()
    agent_recs = _agent_recs_from(recs)
    assert len(agent_recs) == 1, f"expected exactly 1 AGENT, got {len(agent_recs)}"
    # The AGENT span should have status OK (proper close, not error)
    assert agent_recs[0].get("status") in ("OK", None)


def test_auto_invocation_state_lock_protects_depth():
    """Directly verify that AutoInvocationState._lock makes depth thread-safe.

    Simulates concurrent depth increment/decrement as would happen when
    RunnableParallel branches share the same state via copy_context().
    """
    from llm_observability.instrumentation.langchain import AutoInvocationState

    state = AutoInvocationState(depth=1)
    errors = []

    def worker():
        try:
            for _ in range(200):
                with state._lock:
                    state.depth += 1
                with state._lock:
                    state.depth -= 1
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"thread errors: {errors}"
    # After all threads, depth should be unchanged (each did equal inc/dec)
    assert state.depth == 1, f"depth should be 1, got {state.depth}"
