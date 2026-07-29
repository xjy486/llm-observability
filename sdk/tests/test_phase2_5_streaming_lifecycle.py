"""Phase 2.5 final closeout — Streaming lifecycle (P1-2) + Registry cleanup (P1-3)."""
import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import pytest
from llm_observability import Observability
from llm_observability.decorators import agent, chain, task, tool, llm
from llm_observability.context import get_current_context
from llm_observability.span_registry import _sinks as _REGISTRY


def _clean_init(**kwargs):
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="t", endpoint="http://localhost:99999",
                       auto_instrument_openai=False, **kwargs)
    return Observability._tracer


def _records():
    return list(Observability._tracer.reporter._queue)


# ── P1-2: first chunk immediate ──

def test_task_sync_generator_first_chunk_immediate():
    _clean_init()
    state = {"tail": False}
    @agent()
    def outer():
        @chain()
        def gen():
            yield "first"
            state["tail"] = True
            yield "second"
        g = gen()
        assert next(g) == "first"
        assert state["tail"] is False
        return list(g)
    outer()
    Observability.shutdown()


def test_task_async_generator_first_chunk_immediate():
    _clean_init()
    state = {"tail": False}
    @agent()
    def outer():
        @chain()
        async def agen():
            yield "first"
            state["tail"] = True
            yield "second"
        async def run():
            g = agen()
            item = await g.__anext__()
            assert item == "first"
            assert state["tail"] is False
            return [x async for x in g]
        return asyncio.run(run())
    outer()
    Observability.shutdown()


def test_agent_sync_generator_close_restores_context():
    _clean_init()
    @agent()
    def gen():
        yield "first"
        yield "second"
    g = gen()
    next(g)
    g.close()
    assert get_current_context() is None
    assert len(_REGISTRY) == 0
    Observability.shutdown()


def test_agent_async_generator_aclose_restores_context():
    _clean_init()
    @agent()
    async def agen():
        yield "first"
        yield "second"
    async def run():
        g = agen()
        await g.__anext__()
        await g.aclose()
    asyncio.run(run())
    assert get_current_context() is None
    assert len(_REGISTRY) == 0
    Observability.shutdown()


def test_generator_break_restores_context():
    _clean_init()
    @agent()
    def gen():
        for i in range(10):
            yield i
    for item in gen():
        if item >= 2:
            break
    assert get_current_context() is None
    assert len(_REGISTRY) == 0
    Observability.shutdown()


# ── P1-3: registry cleanup ──

def test_task_event_sink_registry_empty_after_1000_calls():
    _clean_init()
    @agent()
    def outer():
        @chain()
        def sub():
            return 1
        return sub()
    for _ in range(1000):
        outer()
    assert len(_REGISTRY) == 0
    Observability.shutdown()


def test_tool_event_sink_registry_empty_after_1000_calls():
    _clean_init()
    @agent()
    def outer():
        @tool()
        def search():
            return "r"
        return search()
    for _ in range(1000):
        outer()
    assert len(_REGISTRY) == 0
    Observability.shutdown()


def test_llm_event_sink_registry_empty_after_1000_calls():
    _clean_init()
    @agent()
    def outer():
        @llm()
        def call_model(messages):
            return "resp"
        return call_model([])
    for _ in range(1000):
        outer()
    assert len(_REGISTRY) == 0
    Observability.shutdown()


def test_stream_close_releases_span_references():
    _clean_init()
    @agent()
    def gen():
        for i in range(5):
            yield i
    g = gen()
    next(g)
    g.close()
    assert len(_REGISTRY) == 0
    Observability.shutdown()


def test_10k_stress_registry_returns_to_zero():
    _clean_init()
    @agent()
    def outer():
        @chain()
        def sub():
            return 1
        return sub()
    for _ in range(10000):
        outer()
    assert len(_REGISTRY) == 0
    Observability.shutdown()
