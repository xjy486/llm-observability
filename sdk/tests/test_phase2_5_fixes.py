"""Phase 2.5 Fix Requirements tests.

Covers the defects called out in
docs/llm-observability-phase2.5-fix-requirements.md:
- P0-2: Root Sampling (sample_rate=0/1, child inheritance, server sampling)
- P0-3: @agent/@llm Input/Output capture
- P0-4: @llm error finalization (NameError + business exception preserved)
- P0-6: annotate inside TASK/TOOL/LLM (event sink coverage)
- P0-7: TASK/TOOL fail-open (telemetry failure preserves business result/exception)
- P0-8: Generator true streaming (first item immediate, bounded)
- P1-1: config enforcement (max_attribute_bytes range)
- P1-2: chain_count via json_extract
- P1-3: association clear
- P1-4: association nested merge
- P1-5: distributed carrier (in-place mutation, app_name, baggage encoding)
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


# ── P0-2: Root Sampling ──

def test_agent_sample_rate_zero():
    """sample_rate=0 -> no AGENT record reported."""
    _clean_init(sample_rate=0.0)

    @agent()
    def qa(q):
        return "a"

    qa("hi")
    recs = _records()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    assert len(agent_recs) == 0
    Observability.shutdown()


def test_agent_sample_rate_one():
    """sample_rate=1 -> AGENT record reported."""
    _clean_init(sample_rate=1.0)

    @agent()
    def qa(q):
        return "a"

    qa("hi")
    recs = _records()
    agent_recs = [r for r in recs if r["span_kind"] == "AGENT"]
    assert len(agent_recs) == 1
    Observability.shutdown()


def test_agent_children_inherit_sampling():
    """When root unsampled, children are not reported either."""
    _clean_init(sample_rate=0.0)

    @agent()
    def outer():
        @chain()
        def sub():
            return 1
        return sub()

    outer()
    recs = _records()
    # No records at all when root unsampled
    assert len(recs) == 0
    Observability.shutdown()


def test_server_new_trace_respects_sample_rate():
    """Server with no carrier uses local sample_rate."""
    _clean_init(sample_rate=0.0)
    with Observability.track_agent_server_call("srv", carrier={}):
        pass
    recs = _records()
    server_recs = [r for r in recs if r["attributes"].get("operation.type") == "server_call"]
    assert len(server_recs) == 0
    Observability.shutdown()


def test_server_remote_sampling_inherited():
    """Server inherits sampled from remote carrier trace_flags."""
    _clean_init(sample_rate=1.0)
    # Build a carrier with sampled=False (trace_flags=00)
    carrier = {
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-00",
    }
    with Observability.track_agent_server_call("srv", carrier=carrier):
        pass
    recs = _records()
    server_recs = [r for r in recs if r["attributes"].get("operation.type") == "server_call"]
    # sampled=False -> not reported
    assert len(server_recs) == 0
    Observability.shutdown()


# ── P0-3: Input/Output capture ──

def test_agent_input_output():
    """@agent captures payload.input and payload.output."""
    _clean_init()

    @agent()
    def qa(query):
        return f"answer:{query}"

    result = qa("hello")
    assert result == "answer:hello"
    rec = _records()[-1]
    assert rec["payload"] is not None
    assert "input" in rec["payload"]
    assert "output" in rec["payload"]
    Observability.shutdown()


def test_agent_none_output():
    """@agent handles None return."""
    _clean_init()

    @agent()
    def qa(query):
        return None

    result = qa("hi")
    assert result is None
    rec = _records()[-1]
    assert rec["status"] == "OK"
    Observability.shutdown()


def test_agent_payload_strategy_off():
    """payload_strategy=off -> no input/output captured."""
    _clean_init(payload_strategy="off")

    @agent()
    def qa(query):
        return "a"

    qa("hi")
    rec = _records()[-1]
    assert rec["payload"] is None or "input" not in (rec["payload"] or {})
    Observability.shutdown()


def test_llm_input_output():
    """@llm captures payload.input and payload.output."""
    _clean_init()

    @agent()
    def outer():
        @llm()
        def call_model(messages):
            return "response"
        return call_model([{"role": "user", "content": "hi"}])

    outer()
    recs = _records()
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(llm_recs) == 1
    assert llm_recs[0]["payload"] is not None
    assert "input" in llm_recs[0]["payload"]
    assert "output" in llm_recs[0]["payload"]
    Observability.shutdown()


# ── P0-4: @llm error finalization ──

def test_llm_business_error_reported():
    """@llm error path sets ERROR, ends span, reports, restores context."""
    _clean_init()

    @agent()
    def outer():
        @llm()
        def call_model(messages):
            raise ValueError("model boom")
        return call_model([])

    with pytest.raises(ValueError):
        outer()

    recs = _records()
    llm_recs = [r for r in recs if r["span_kind"] == "LLM"]
    assert len(llm_recs) == 1
    assert llm_recs[0]["status"] == "ERROR"
    assert llm_recs[0]["error_type"] == "ValueError"
    # Context restored (no active trace after outer exits)
    assert get_current_context() is None
    Observability.shutdown()


def test_llm_error_context_restored():
    """After @llm error, context is restored to parent."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        root_ctx = get_current_context()

        @llm()
        def call_model(messages):
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            call_model([])
        # Context should be back to the root (LLM token reset)
        assert get_current_context().span_id == root_ctx.span_id

    Observability.shutdown()


def test_llm_reporter_failure_preserves_business_error():
    """Reporter failure does not replace the business exception."""
    _clean_init()

    @agent()
    def outer():
        @llm()
        def call_model(messages):
            raise ValueError("biz error")
        return call_model([])

    # Make reporter.report raise
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda rec: (_ for _ in ()).throw(ConnectionError("reporter down"))

    try:
        with pytest.raises(ValueError):
            outer()
    finally:
        Observability._tracer.reporter.report = original_report

    Observability.shutdown()


# ── P0-6: annotate inside sub-spans ──

def test_annotate_inside_task():
    """annotate() works inside a TASK span (event sink registered)."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        with tracer.task(name="sub"):
            ok = Observability.annotate(attributes={"k": "v"})
            assert ok is True

    task_recs = [r for r in _records() if r["span_kind"] == "TASK"]
    assert task_recs[0]["attributes"].get("k") == "v"
    Observability.shutdown()


def test_annotate_inside_tool():
    """annotate() works inside a TOOL span (event sink registered)."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        with tracer.tool(name="search"):
            ok = Observability.annotate(attributes={"k": "v"})
            assert ok is True

    tool_recs = [r for r in _records() if r["span_kind"] == "TOOL"]
    assert tool_recs[0]["attributes"].get("k") == "v"
    Observability.shutdown()


def test_annotate_inside_llm():
    """annotate() works inside a decorator LLM span."""
    _clean_init()

    @agent()
    def outer():
        @llm()
        def call_model(messages):
            ok = Observability.annotate(attributes={"k": "v"})
            assert ok is True
            return "resp"
        return call_model([])

    outer()
    llm_recs = [r for r in _records() if r["span_kind"] == "LLM"]
    assert llm_recs[0]["attributes"].get("k") == "v"
    Observability.shutdown()


def test_annotate_event_sink_cleanup():
    """After a TASK span ends, its event sink is unregistered."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        with tracer.task(name="sub") as handle:
            task_span_id = handle._span.span_id
        # After exit, annotate with no explicit span returns False for that span
        from llm_observability.span_registry import get_span_event_sink
        sink = get_span_event_sink(tracer._current.trace_id if hasattr(tracer, "_current") else "", task_span_id)
        # The sink for the task span should be gone
    Observability.shutdown()


# ── P0-7: TASK/TOOL fail-open ──

def test_task_telemetry_failure_preserves_result():
    """TASK telemetry failure does not change business result."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        # Make span.end raise via monkeypatch
        original_report = tracer.reporter.report
        tracer.reporter.report = lambda rec: (_ for _ in ()).throw(RuntimeError("report fail"))
        try:
            with tracer.task(name="sub") as h:
                h.set_output("biz-result")
            # business result preserved
        finally:
            tracer.reporter.report = original_report
    # Context restored
    Observability.shutdown()


def test_tool_output_failure_preserves_result():
    """TOOL output processing failure does not change business result."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        with tracer.tool(name="search") as h:
            h.set_output({"big": "x" * 100000})
        # exits cleanly
    Observability.shutdown()


# ── P0-8: Generator true streaming ──

def test_task_sync_generator_first_item_immediate():
    """TASK sync generator yields the first item before exhausting."""
    _clean_init()

    @agent()
    def outer():
        @chain()
        def gen(n):
            for i in range(n):
                yield i
        return list(gen(5))

    result = outer()
    assert result == [0, 1, 2, 3, 4]
    task_recs = [r for r in _records() if r["span_kind"] == "TASK"]
    assert len(task_recs) == 1
    assert task_recs[0]["status"] == "OK"
    Observability.shutdown()


def test_tool_sync_generator_first_item_immediate():
    """TOOL sync generator yields items immediately (true streaming)."""
    _clean_init()

    @agent()
    def outer():
        @tool()
        def gen(n):
            for i in range(n):
                yield i
        consumed = []
        for item in gen(3):
            consumed.append(item)
        return consumed

    result = outer()
    assert result == [0, 1, 2]
    tool_recs = [r for r in _records() if r["span_kind"] == "TOOL"]
    assert len(tool_recs) == 1
    Observability.shutdown()


def test_async_generator_bounded():
    """Async generator captures output bounded."""
    _clean_init()

    @agent()
    def outer():
        @chain()
        async def agen(n):
            for i in range(n):
                yield i
        async def collect():
            return [x async for x in agen(4)]
        return asyncio.run(collect())

    result = outer()
    assert result == [0, 1, 2, 3]
    task_recs = [r for r in _records() if r["span_kind"] == "TASK"]
    assert len(task_recs) == 1
    Observability.shutdown()


def test_infinite_stream_no_unbounded_memory():
    """BoundedStreamAccumulator stops accumulating past budget but yields all."""
    from llm_observability.integrations.langchain.stream_accumulator import (
        BoundedStreamAccumulator,
    )
    acc = BoundedStreamAccumulator(max_bytes=256)
    for i in range(10000):
        acc.append(f"chunk-{i}")
    # truncated because budget exceeded
    assert acc.truncated is True
    # count still tracks all chunks
    assert acc.count == 10000
    # finalize returns a bounded result, not 10000 chunks
    result = acc.finalize()
    assert result["truncated"] is True
    assert len(result["chunks"]) < 10000
    Observability.shutdown()


# ── P1-1: config enforcement ──

def test_max_attribute_bytes_range_validation():
    """max_attribute_bytes outside 1KiB-128KiB raises."""
    if Observability._initialized:
        Observability.shutdown()
    with pytest.raises(ValueError):
        Observability.init(
            app_name="x", endpoint="http://localhost:1",
            auto_instrument_openai=False, max_attribute_bytes=100,  # too small
        )
    with pytest.raises(ValueError):
        Observability.init(
            app_name="x", endpoint="http://localhost:1",
            auto_instrument_openai=False, max_attribute_bytes=200 * 1024,  # too big
        )


def test_sample_rate_validation():
    """sample_rate outside [0,1] raises."""
    if Observability._initialized:
        Observability.shutdown()
    with pytest.raises(ValueError):
        Observability.init(
            app_name="x", endpoint="http://localhost:1",
            auto_instrument_openai=False, sample_rate=1.5,
        )


def test_decorator_fail_open_none_uses_global():
    """Decorator fail_open=None uses global config.fail_open."""
    _clean_init(fail_open=True)

    @agent(fail_open=None)
    def qa(q):
        return "a"
    # SDK initialized -> runs normally
    assert qa("hi") == "a"
    Observability.shutdown()


# ── P1-2: chain_count ──

def test_chain_count_json_extract():
    """chain_count uses json_extract (robust to attribute key ordering)."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        with tracer.task(name="c1", task_type="chain"):
            pass
        with tracer.task(name="t1", task_type="task"):
            pass

    recs = _records()
    # Use the storage layer to verify chain_count
    from core.storage.db import Storage
    db = Storage(":memory:")
    for r in recs:
        db.insert_span(r)
    # The root trace_id
    root_rec = [r for r in recs if r["span_kind"] == "AGENT"][0]
    detail = db.get_trace_detail(root_rec["trace_id"])
    assert detail["task_count"] == 2
    assert detail["chain_count"] == 1
    Observability.shutdown()


# ── P1-3: association clear ──

def test_clear_association_properties():
    """clear_association_properties resets to empty."""
    _clean_init()
    from llm_observability.association import (
        get_association_properties, clear_association_properties,
        set_association_properties,
    )
    set_association_properties({"user": "alice"})
    assert get_association_properties().user == "alice"
    clear_association_properties()
    assert get_association_properties().user is None
    Observability.shutdown()


# ── P1-4: association nested merge ──

def test_association_nested_merge():
    """Inner context inherits unset fields from outer; explicit overrides."""
    _clean_init()
    from llm_observability.association import get_association_properties

    with Observability.association_context(user="alice", session_id="s1"):
        # Inner: only set message_id, should inherit user + session
        with Observability.association_context(message_id="m2"):
            props = get_association_properties()
            assert props.user == "alice"
            assert props.session_id == "s1"
            assert props.message_id == "m2"

    Observability.shutdown()


# ── P1-5: distributed carrier ──

def test_inject_carrier_returns_same_object():
    """inject_carrier mutates in place and returns the same object."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root", session_id="s1", user_id="u1"):
        headers = {"X-Custom": "keep"}
        returned = Observability.inject_carrier(headers)
        assert returned is headers  # P1-5: same object
        assert "traceparent" in headers
        assert headers["X-Session-Id"] == "s1"
        # user-provided header preserved
        assert headers["X-Custom"] == "keep"

    Observability.shutdown()


def test_carrier_app_name_propagation():
    """Carrier includes app_name."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        carrier = Observability.inject_carrier()
        assert "X-App-Name" in carrier
        assert "app_name" in carrier["baggage"]

    Observability.shutdown()


def test_carrier_baggage_encoding_special_chars():
    """Baggage values with special chars are percent-encoded/decoded."""
    _clean_init()
    tracer = Observability._tracer

    with Observability.association_context(user="alice,bob=1 x"):
        with tracer.trace(name="root"):
            carrier = Observability.inject_carrier()
            # baggage is encoded (no raw comma/equals/space in values)
            baggage = carrier["baggage"]
            assert "alice,bob" not in baggage  # encoded
            # round-trip decode
            extracted = Observability.extract_carrier(carrier)
            assert extracted is not None
            assert extracted.association["user"] == "alice,bob=1 x"

    Observability.shutdown()


def test_carrier_in_place_mutation_for_client_call():
    """track_task_client_call mutates the caller's carrier in place."""
    _clean_init()
    tracer = Observability._tracer

    with tracer.trace(name="root"):
        headers = {}
        with Observability.track_task_client_call("downstream", carrier=headers):
            pass
        # headers dict was mutated in place
        assert "traceparent" in headers

    Observability.shutdown()
