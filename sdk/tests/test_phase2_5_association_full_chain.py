"""Phase 2.5 final closeout — Association full-chain (P0-3)."""
import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import pytest
from llm_observability import Observability
from llm_observability.decorators import agent, chain, task, tool, llm
from llm_observability.association import get_association_properties


def _clean_init(**kwargs):
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="t", endpoint="http://localhost:99999",
                       auto_instrument_openai=False, **kwargs)
    return Observability._tracer


def _records():
    return list(Observability._tracer.reporter._queue)


def test_agent_explicit_association_inherited_by_task():
    _clean_init()
    @agent(user_id="alice", session_id="s1", message_id="m1", business_scenario="cs")
    def outer():
        @chain()
        def sub():
            return 1
        return sub()
    outer()
    recs = _records()
    task_recs = [r for r in recs if r["span_kind"] == "TASK"]
    assert task_recs[0].get("message_id") == "m1"
    assert task_recs[0].get("user_id") == "alice"
    assert task_recs[0].get("session_id") == "s1"
    assert task_recs[0].get("business_scene") == "cs"
    Observability.shutdown()


def test_agent_explicit_association_inherited_by_tool():
    _clean_init()
    @agent(user_id="alice", message_id="m1")
    def outer():
        @tool()
        def search():
            return "r"
        return search()
    outer()
    recs = _records()
    tool_recs = [r for r in recs if r["span_kind"] == "TOOL"]
    assert tool_recs[0].get("message_id") == "m1"
    assert tool_recs[0].get("user_id") == "alice"
    Observability.shutdown()


def test_agent_explicit_association_inherited_by_llm():
    _clean_init()
    @agent(user_id="alice", message_id="m1")
    def outer():
        @llm()
        def call_model(messages):
            return "resp"
        return call_model([])
    outer()
    recs = _records()
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert llm_recs[0].get("message_id") == "m1"
    assert llm_recs[0].get("user_id") == "alice"
    Observability.shutdown()


def test_agent_explicit_association_nested_merge():
    _clean_init()
    # Outer association context + @agent explicit — agent explicit wins
    with Observability.association_context(user="outer-user", session_id="outer-sess"):
        @agent(user_id="alice")
        def outer():
            # Inside agent: user=alice (explicit), session_id=outer-sess (inherited)
            from llm_observability.association import get_association_properties
            props = get_association_properties()
            assert props.user == "alice"
            assert props.session_id == "outer-sess"
            return 1
        outer()
    Observability.shutdown()


def test_agent_association_restored_after_success():
    _clean_init()
    @agent(user_id="alice")
    def outer():
        return 1
    outer()
    # After exit, association context restored to empty
    props = get_association_properties()
    assert props.user is None
    Observability.shutdown()


def test_agent_association_restored_after_error():
    _clean_init()
    @agent(user_id="alice")
    def outer():
        raise ValueError("boom")
    with pytest.raises(ValueError):
        outer()
    props = get_association_properties()
    assert props.user is None
    Observability.shutdown()


def test_agent_association_restored_after_generator_close():
    _clean_init()
    @agent(user_id="alice")
    def gen():
        yield "first"
        yield "second"
    g = gen()
    assert next(g) == "first"
    g.close()
    props = get_association_properties()
    assert props.user is None
    Observability.shutdown()


def test_agent_association_restored_after_async_generator_aclose():
    _clean_init()
    @agent(user_id="alice")
    async def agen():
        yield "first"
        yield "second"
    async def run():
        g = agen()
        item = await g.__anext__()
        assert item == "first"
        await g.aclose()
    asyncio.run(run())
    props = get_association_properties()
    assert props.user is None
    Observability.shutdown()
