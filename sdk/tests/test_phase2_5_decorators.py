"""Phase 2.5 — AgentLens SDK Parity tests.

Covers: @agent/@chain/@task/@tool/@llm decorators (sync/async/generator),
annotate, association properties, instruments/block_instruments, distributed
client/server, and fail-open behavior.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest

from llm_observability import Observability
from llm_observability.decorators import agent, chain, task, tool, llm
from llm_observability.context import get_current_context
from llm_observability.spans import SpanKind


def _clean_init(**kwargs):
    """Ensure clean state before init."""
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(
        app_name="test-app",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
        **kwargs,
    )
    return Observability._tracer


def _records():
    return list(Observability._tracer.reporter._queue)


# ── @agent decorator ──

def test_agent_decorator_sync():
    """@agent creates an AGENT root span for a sync function."""
    tracer = _clean_init()

    @agent()
    def qa_agent(query):
        return f"answer to {query}"

    result = qa_agent("hello")
    assert result == "answer to hello"

    recs = _records()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    assert len(agent_recs) == 1
    assert agent_recs[0]["status"] == "OK"
    assert agent_recs[0]["parent_span_id"] is None
    assert agent_recs[0]["attributes"].get("operation.type") == "agent"

    Observability.shutdown()


def test_agent_decorator_exception_reraised():
    """@agent marks ERROR and re-raises business exception."""
    tracer = _clean_init()

    @agent()
    def bad_agent():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        bad_agent()

    recs = _records()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    assert agent_recs[0]["status"] == "ERROR"
    assert agent_recs[0]["error_type"] == "ValueError"

    Observability.shutdown()


def test_agent_decorator_nested_error():
    """@agent nested_mode='error' raises when a trace is already active."""
    tracer = _clean_init()

    @agent(nested_mode="error")
    def inner():
        return 1

    @agent()
    def outer():
        return inner()

    with pytest.raises(RuntimeError):
        outer()

    Observability.shutdown()


def test_agent_decorator_nested_reuse():
    """@agent nested_mode='reuse' reuses the current trace."""
    tracer = _clean_init()

    @agent(nested_mode="reuse")
    def inner():
        return 1

    @agent()
    def outer():
        return inner()

    result = outer()
    assert result == 1

    recs = _records()
    # Only one AGENT root (reused, not a second)
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT" and r["parent_span_id"] is None]
    assert len(agent_recs) == 1

    Observability.shutdown()


def test_agent_decorator_async():
    """@agent works for async functions."""
    tracer = _clean_init()

    @agent()
    async def qa_agent(query):
        return f"answer to {query}"

    result = asyncio.run(qa_agent("hello"))
    assert result == "answer to hello"

    recs = _records()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    assert len(agent_recs) == 1
    assert agent_recs[0]["status"] == "OK"

    Observability.shutdown()


def test_agent_decorator_sync_generator():
    """@agent works for sync generators."""
    tracer = _clean_init()

    @agent()
    def gen_agent(n):
        for i in range(n):
            yield i

    result = list(gen_agent(3))
    assert result == [0, 1, 2]

    recs = _records()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    assert len(agent_recs) == 1
    assert agent_recs[0]["status"] == "OK"

    Observability.shutdown()


def test_agent_decorator_async_generator():
    """@agent works for async generators."""
    tracer = _clean_init()

    @agent()
    async def agen_agent(n):
        for i in range(n):
            yield i

    async def collect():
        return [x async for x in agen_agent(3)]

    result = asyncio.run(collect())
    assert result == [0, 1, 2]

    recs = _records()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    assert len(agent_recs) == 1

    Observability.shutdown()


# ── @chain / @task decorators ──

def test_chain_decorator_creates_task_span():
    """@chain creates a TASK span with task.type=chain."""
    tracer = _clean_init()

    @agent()
    def outer():
        @chain()
        def pipeline(doc):
            return doc.upper()
        return pipeline("hello")

    result = outer()
    assert result == "HELLO"

    recs = _records()
    task_recs = [r for r in recs if r["span_kind"] == "TASK"]
    assert len(task_recs) == 1
    assert task_recs[0]["attributes"].get("task.type") == "chain"
    assert task_recs[0]["parent_span_id"] is not None  # child of AGENT

    Observability.shutdown()


def test_task_decorator_creates_task_span():
    """@task creates a TASK span with task.type=task."""
    tracer = _clean_init()

    @agent()
    def outer():
        @task()
        def html_to_md(html):
            return html.replace("<b>", "**")
        return html_to_md("<b>hi")

    result = outer()
    assert result == "**hi"

    recs = _records()
    task_recs = [r for r in recs if r["span_kind"] == "TASK"]
    assert len(task_recs) == 1
    assert task_recs[0]["attributes"].get("task.type") == "task"

    Observability.shutdown()


def test_chain_no_trace_fail_open():
    """@chain without a trace + fail_open=True runs without observation."""
    _clean_init()

    @chain()
    def pipeline(doc):
        return doc.upper()

    # No active trace — fail-open, business proceeds
    result = pipeline("hello")
    assert result == "HELLO"
    assert len(_records()) == 0

    Observability.shutdown()


def test_chain_no_trace_fail_closed():
    """@chain without a trace + fail_open=False raises."""
    _clean_init()

    @chain(fail_open=False)
    def pipeline(doc):
        return doc.upper()

    with pytest.raises(RuntimeError):
        pipeline("hello")

    Observability.shutdown()


def test_task_nested_in_task():
    """TASK can nest TASK."""
    tracer = _clean_init()

    @agent()
    def outer():
        @task()
        def parent_task():
            @task()
            def child_task():
                return 42
            return child_task()
        return parent_task()

    result = outer()
    assert result == 42

    recs = _records()
    task_recs = [r for r in recs if r["span_kind"] == "TASK"]
    assert len(task_recs) == 2

    Observability.shutdown()


# ── @tool decorator ──

def test_tool_decorator_creates_tool_span():
    """@tool creates a TOOL span."""
    tracer = _clean_init()

    @agent()
    def outer():
        @tool()
        def search(query):
            return [f"result-{query}"]
        return search("test")

    result = outer()
    assert result == ["result-test"]

    recs = _records()
    tool_recs = [r for r in recs if r["span_kind"] == "TOOL"]
    assert len(tool_recs) == 1

    Observability.shutdown()


# ── @llm decorator ──

def test_llm_decorator_creates_llm_span():
    """@llm creates an LLM span with logical_llm_span_active set."""
    tracer = _clean_init()

    @agent()
    def outer():
        @llm()
        def call_model(messages):
            return "response"
        return call_model([{"role": "user", "content": "hi"}])

    result = outer()
    assert result == "response"

    recs = _records()
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(llm_recs) == 1
    assert llm_recs[0]["attributes"].get("gen_ai.operation.name") == "chat"

    Observability.shutdown()


def test_llm_decorator_no_trace_fail_open():
    """@llm without a trace + fail_open runs without observation."""
    _clean_init()

    @llm()
    def call_model(messages):
        return "response"

    result = call_model([])
    assert result == "response"
    assert len(_records()) == 0

    Observability.shutdown()


# ── annotate ──

def test_annotate_current_span():
    """annotate() annotates the current active span."""
    tracer = _clean_init()

    with tracer.trace(name="my-task"):
        Observability.annotate(
            attributes={"custom.key": "value"},
            tags=["important"],
        )

    rec = _records()[-1]
    assert rec["attributes"].get("custom.key") == "value"
    assert rec["attributes"].get("sdk.tags") == ["important"]

    Observability.shutdown()


def test_annotate_overwrites_input_output():
    """annotate() overwrites captured input/output."""
    tracer = _clean_init()

    with tracer.trace(name="my-task"):
        Observability.annotate(
            input_data={"q": "hello"},
            output_data={"a": "world"},
        )

    rec = _records()[-1]
    assert rec["payload"]["input"] is not None
    assert rec["payload"]["output"] is not None

    Observability.shutdown()


def test_annotate_protected_keys_ignored():
    """annotate() cannot overwrite protected keys."""
    tracer = _clean_init()

    with tracer.trace(name="my-task"):
        Observability.annotate(attributes={"trace_id": "hacked"})

    rec = _records()[-1]
    # trace_id unchanged
    assert rec["trace_id"] != "hacked"

    Observability.shutdown()


def test_annotate_no_span_fail_open():
    """annotate() with no active span returns False (fail-open)."""
    _clean_init()
    result = Observability.annotate(attributes={"x": 1})
    assert result is False
    Observability.shutdown()


# ── Association Properties ──

def test_association_context_inherited_by_spans():
    """Association properties are inherited by all spans."""
    tracer = _clean_init()

    with Observability.association_context(
        user="alice", session_id="s1", message_id="m1", business_scenario="cs",
    ):
        with tracer.trace(name="my-task"):
            @task()
            def sub_task():
                return 1
            sub_task()

    recs = _records()
    for r in recs:
        assert r.get("message_id") == "m1"

    Observability.shutdown()


def test_association_alias_normalization():
    """user_id and business_scene aliases are normalized."""
    tracer = _clean_init()

    token = Observability.set_association_properties({
        "user_id": "bob", "business_scene": "sales",
    })
    try:
        with tracer.trace(name="my-task"):
            pass
    finally:
        Observability.reset_association_properties(token)

    rec = _records()[-1]
    assert rec["user_id"] == "bob"
    assert rec["business_scene"] == "sales"

    Observability.shutdown()


def test_association_context_cleanup_on_exception():
    """Association context is restored after exception."""
    _clean_init()

    with pytest.raises(ValueError):
        with Observability.association_context(user="alice"):
            raise ValueError("boom")

    # After the block, association should be empty
    from llm_observability.association import get_association_properties
    props = get_association_properties()
    assert props.user is None

    Observability.shutdown()


# ── Instruments ──

def test_instruments_enum():
    """Instruments enum has OPENAI and LANGCHAIN."""
    from llm_observability.instruments import Instruments
    assert Instruments.OPENAI == "openai"
    assert Instruments.LANGCHAIN == "langchain"


def test_block_instruments_openai():
    """block_instruments={OPENAI} prevents OpenAI auto-instrumentation."""
    from llm_observability.instruments import Instruments
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(
        app_name="test-app",
        endpoint="http://localhost:99999",
        auto_instrument_openai=True,
        block_instruments={Instruments.OPENAI},
    )

    # OpenAI should not be instrumented
    assert Observability._openai_instrumentor is None
    assert "openai" not in Observability._instrument_manager.active

    Observability.shutdown()


def test_instrument_manager_idempotent():
    """Repeated instrument() is idempotent."""
    _clean_init()
    mgr = Observability._instrument_manager
    # OpenAI already not instrumented (auto off); instrument twice
    mgr.instrument("openai")
    mgr.instrument("openai")
    assert "openai" in mgr.active

    Observability.shutdown()


def test_single_instrument_failure_isolated():
    """A failed instrument does not affect others."""
    _clean_init()
    mgr = Observability._instrument_manager
    # Unknown instrument fails gracefully
    ok = mgr.instrument("nonexistent_module")
    assert ok is False
    # Manager still works
    assert isinstance(mgr.active, list)

    Observability.shutdown()


# ── Distributed ──

def test_inject_extract_carrier_roundtrip():
    """inject_carrier/extract_carrier round-trips trace context."""
    tracer = _clean_init()

    with tracer.trace(name="my-task", session_id="s1", user_id="u1"):
        carrier = Observability.inject_carrier()
        assert "traceparent" in carrier
        assert carrier.get("X-Session-Id") == "s1"
        assert carrier.get("X-User-Id") == "u1"

        extracted = Observability.extract_carrier(carrier)
        assert extracted is not None
        assert extracted.inherited is True

    Observability.shutdown()


def test_extract_carrier_invalid_returns_none():
    """extract_carrier returns None for invalid traceparent."""
    _clean_init()
    result = Observability.extract_carrier({"traceparent": "garbage"})
    assert result is None

    result = Observability.extract_carrier({})
    assert result is None
    Observability.shutdown()


def test_client_server_same_trace():
    """Client TASK and Server AGENT share the same TraceID."""
    tracer = _clean_init()

    # Client side
    with tracer.trace(name="client-root"):
        headers = {}
        with Observability.track_task_client_call("downstream", carrier=headers) as client_span:
            client_span.set_output("ok")

        # Server side extracts the carrier
        with Observability.track_agent_server_call("server-handler", carrier=headers):
            pass

    recs = _records()
    client_call = [r for r in recs if r["span_kind"] == "TASK" and r["attributes"].get("task.type") == "client_call"]
    server_call = [r for r in recs if r["span_kind"] == "AGENT" and r["attributes"].get("operation.type") == "server_call"]

    assert len(client_call) == 1
    assert len(server_call) == 1
    # Same trace
    assert client_call[0]["trace_id"] == server_call[0]["trace_id"]
    # Server parent = client span
    assert server_call[0]["parent_span_id"] == client_call[0]["span_id"]

    Observability.shutdown()


def test_server_invalid_carrier_creates_new_trace():
    """Server with invalid carrier creates a new trace (fail-open)."""
    tracer = _clean_init()

    with Observability.track_agent_server_call("server", carrier={}):
        pass

    recs = _records()
    server_recs = [r for r in recs if r["attributes"].get("operation.type") == "server_call"]
    assert len(server_recs) == 1
    assert server_recs[0]["parent_span_id"] is None  # new trace

    Observability.shutdown()


# ── Fail-open ──

def test_decorator_not_initialized_fail_open():
    """Decorators run business only when SDK not init + fail_open=True."""
    if Observability._initialized:
        Observability.shutdown()

    @agent()
    def qa(query):
        return f"answer {query}"

    # Should run without observation (no init)
    result = qa("hi")
    assert result == "answer hi"


def test_decorator_not_initialized_fail_closed():
    """Decorators raise when SDK not init + fail_open=False."""
    if Observability._initialized:
        Observability.shutdown()

    @agent(fail_open=False)
    def qa(query):
        return f"answer {query}"

    with pytest.raises(RuntimeError):
        qa("hi")


def test_span_kind_task_constant():
    """TASK SpanKind is defined."""
    assert SpanKind.TASK == "TASK"
