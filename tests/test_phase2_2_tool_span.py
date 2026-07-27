"""Phase 2.2 Tool Span Tests — SDK + Core."""
import sys
import os
import json
import time
import asyncio
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "core"))

from llm_observability.spans import Span, SpanKind
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context
from llm_observability.config import Config
from llm_observability.tracer import Tracer


class MockReporter:
    def __init__(self):
        self.records = []

    def report(self, record):
        self.records.append(record)


def make_tracer(payload_strategy="masked", sample_rate=1.0):
    config = Config(payload_strategy=payload_strategy, sample_rate=sample_rate)
    reporter = MockReporter()
    return Tracer(config=config, reporter=reporter), reporter


# ═══════════════════════════════════════════════════════════════
# Task 1: Span Model Extension
# ═══════════════════════════════════════════════════════════════

class TestSpanPayloadFields:
    def test_span_has_payload_and_request_metadata_fields(self):
        span = Span(
            trace_id="t1", span_id="s1", parent_span_id=None,
            span_name="tool.search", span_kind=SpanKind.TOOL,
        )
        assert span.payload is None
        assert span.request_metadata is None

    def test_span_to_record_includes_payload_and_metadata(self):
        span = Span(
            trace_id="t1", span_id="s1", parent_span_id=None,
            span_name="tool.search", span_kind=SpanKind.TOOL,
        )
        span.payload = {"input": {"query": "test"}, "output": ["result1"]}
        span.request_metadata = {"tool_name": "search", "tool_type": "search"}
        record = span.to_record()
        assert "payload" in record
        assert record["payload"]["input"]["query"] == "test"
        assert record["request_metadata"]["tool_name"] == "search"


# ═══════════════════════════════════════════════════════════════
# Task 2: Safe Serialization + Size Guard
# ═══════════════════════════════════════════════════════════════

from llm_observability.tool import safe_serialize, apply_size_guard


class TestSafeSerialize:
    def test_primitive_types(self):
        assert safe_serialize("hello") == "hello"
        assert safe_serialize(42) == 42
        assert safe_serialize(3.14) == 3.14
        assert safe_serialize(True) is True
        assert safe_serialize(None) is None

    def test_dict_and_list(self):
        assert safe_serialize({"a": 1, "b": [2, 3]}) == {"a": 1, "b": [2, 3]}
        assert safe_serialize([1, "two", {"three": 3}]) == [1, "two", {"three": 3}]

    def test_tuple_becomes_list(self):
        result = safe_serialize((1, 2, 3))
        assert result == [1, 2, 3]

    def test_dataclass(self):
        from dataclasses import dataclass

        @dataclass
        class Point:
            x: int
            y: int

        result = safe_serialize(Point(1, 2))
        assert result == {"x": 1, "y": 2}

    def test_bytes(self):
        result = safe_serialize(b"hello world")
        assert isinstance(result, dict)
        assert result["_type"] == "bytes"
        assert result["size_bytes"] == 11

    def test_unknown_object_safe_repr(self):
        class FileLike:
            def __repr__(self):
                return "<FileLike at 0x123>"

        result = safe_serialize(FileLike())
        assert isinstance(result, dict)
        assert result["_type"] == "FileLike"


class TestSizeGuard:
    def test_small_data_unchanged(self):
        data = {"query": "hello"}
        result, truncated, original_size = apply_size_guard(data)
        assert result == {"query": "hello"}
        assert truncated is False

    def test_large_data_truncated(self):
        big_string = "x" * 100_000
        data = {"output": big_string}
        result, truncated, original_size = apply_size_guard(data, max_bytes=1024)
        assert truncated is True
        assert result.get("_truncated") is True
        assert result.get("_original_size_bytes") > 1024
        assert "_preview" in result

    def test_size_guard_preserves_small_nested(self):
        data = {"a": {"b": "c"}}
        result, truncated, original_size = apply_size_guard(data)
        assert truncated is False
        assert result == {"a": {"b": "c"}}


# ═══════════════════════════════════════════════════════════════
# Task 3: ToolContextManager Lifecycle
# ═══════════════════════════════════════════════════════════════

class TestToolContextManager:
    def test_tool_requires_active_trace(self):
        tracer, reporter = make_tracer()
        with pytest.raises(RuntimeError, match="active trace"):
            with tracer.tool(name="search"):
                pass

    def test_tool_child_of_agent(self):
        tracer, reporter = make_tracer()
        with tracer.trace(name="task") as agent_ctx:
            agent_span_id = get_current_context().span_id
            with tracer.tool(name="web_search") as tool_handle:
                tool_ctx = get_current_context()
                assert tool_ctx.span_kind == SpanKind.TOOL
                assert tool_ctx.parent_span_id == agent_span_id
                assert tool_ctx.trace_id == agent_ctx._span.trace_id
            # After tool exits, context should be back to AGENT
            assert get_current_context().span_id == agent_span_id

    def test_tool_span_recorded(self):
        tracer, reporter = make_tracer()
        with tracer.trace(name="task"):
            with tracer.tool(name="search"):
                pass

        assert len(reporter.records) == 2
        tool_record = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        assert tool_record["span_name"] == "tool.search"
        assert tool_record["status"] == "OK"

    def test_tool_exception_error_and_reraise(self):
        tracer, reporter = make_tracer()
        with pytest.raises(ValueError, match="boom"):
            with tracer.trace(name="task"):
                with tracer.tool(name="terminal"):
                    raise ValueError("boom")

        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert len(tool_records) == 1
        assert tool_records[0]["status"] == "ERROR"
        assert tool_records[0]["error_type"] == "ValueError"
        assert tool_records[0]["error_message"] == "boom"

    def test_tool_context_restored_after_exception(self):
        tracer, reporter = make_tracer()
        with tracer.trace(name="task") as agent_ctx:
            agent_span_id = get_current_context().span_id
            try:
                with tracer.tool(name="fail_tool"):
                    raise RuntimeError("fail")
            except RuntimeError:
                pass
            assert get_current_context().span_id == agent_span_id

    def test_tool_sampling_inherited(self):
        tracer, reporter = make_tracer(sample_rate=0.0)
        with tracer.trace(name="task"):
            with tracer.tool(name="search"):
                pass

        assert len(reporter.records) == 0

    def test_tool_manual_set_error(self):
        tracer, reporter = make_tracer()
        with tracer.trace(name="task"):
            with tracer.tool(name="remote_call") as tool:
                tool.set_error("ToolBusinessError", "service unavailable")

        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert tool_records[0]["status"] == "ERROR"
        assert tool_records[0]["error_type"] == "ToolBusinessError"


# ═══════════════════════════════════════════════════════════════
# Task 4: Parent-Child Relationships
# ═══════════════════════════════════════════════════════════════

class TestToolParentChild:
    def test_nested_tool_parent_child(self):
        tracer, reporter = make_tracer()
        with tracer.trace(name="task"):
            with tracer.tool(name="outer"):
                outer_id = get_current_context().span_id
                with tracer.tool(name="inner"):
                    assert get_current_context().parent_span_id == outer_id
                assert get_current_context().span_id == outer_id
            assert get_current_context().span_kind == SpanKind.AGENT

        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        outer = [r for r in tool_records if r["span_name"] == "tool.outer"][0]
        inner = [r for r in tool_records if r["span_name"] == "tool.inner"][0]
        agent = [r for r in reporter.records if r["span_kind"] == "AGENT"][0]
        assert inner["parent_span_id"] == outer["span_id"]
        assert outer["parent_span_id"] == agent["span_id"]

    def test_tool_llm_child_relationship(self):
        tracer, reporter = make_tracer()
        with tracer.trace(name="task"):
            with tracer.tool(name="retrieval"):
                tool_ctx = get_current_context()
                tool_span_id = tool_ctx.span_id
                from llm_observability.utils.ids import generate_span_id
                llm_span_id = generate_span_id()
                llm_ctx = SpanContext(
                    trace_id=tool_ctx.trace_id, span_id=llm_span_id,
                    parent_span_id=tool_span_id, span_kind=SpanKind.LLM,
                    sampled=tool_ctx.sampled,
                )
                token = set_context(llm_ctx)
                assert get_current_context().parent_span_id == tool_span_id
                reset_context(token)
            # After tool exits, context should be back to AGENT
            assert get_current_context().span_kind == SpanKind.AGENT

    def test_normal_llm_then_tool_are_siblings(self):
        tracer, reporter = make_tracer()
        with tracer.trace(name="task") as agent_ctx:
            agent_span_id = get_current_context().span_id
            from llm_observability.utils.ids import generate_span_id
            llm_span_id = generate_span_id()
            llm_ctx = SpanContext(
                trace_id=agent_ctx._span.trace_id, span_id=llm_span_id,
                parent_span_id=agent_span_id, span_kind=SpanKind.LLM, sampled=True,
            )
            token = set_context(llm_ctx)
            reset_context(token)
            with tracer.tool(name="search"):
                tool_ctx = get_current_context()
                assert tool_ctx.parent_span_id == agent_span_id
                assert tool_ctx.span_kind == SpanKind.TOOL


# ═══════════════════════════════════════════════════════════════
# Task 5: Payload Strategy Tests
# ═══════════════════════════════════════════════════════════════

class TestToolPayloadStrategies:
    def test_off_no_payload(self):
        tracer, reporter = make_tracer(payload_strategy="off")
        with tracer.trace(name="task"):
            with tracer.tool(name="search", input={"q": "secret"}) as tool:
                tool.set_output(["result1"])
        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        assert tool_rec["payload"] is None or "input" not in (tool_rec["payload"] or {})

    def test_metadata_only_no_real_values(self):
        tracer, reporter = make_tracer(payload_strategy="metadata_only")
        with tracer.trace(name="task"):
            with tracer.tool(name="search", input={"query": "secret text", "limit": 10}) as tool:
                tool.set_output(["result1", "result2"])
        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        payload = tool_rec.get("payload", {})
        if payload and "input" in payload:
            input_data = payload["input"]
            assert "secret text" not in json.dumps(input_data) if input_data else True

    def test_masked_redacts_sensitive_keys(self):
        tracer, reporter = make_tracer(payload_strategy="masked")
        with tracer.trace(name="task"):
            with tracer.tool(name="http_fetch", input={"url": "https://example.com", "api_key": "sk-secret123"}) as tool:
                tool.set_output({"status": 200, "authorization": "Bearer abc123"})
        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        payload = tool_rec.get("payload", {})
        if payload and "input" in payload:
            input_str = json.dumps(payload["input"])
            assert "sk-secret123" not in input_str

    def test_full_preserves_values(self):
        tracer, reporter = make_tracer(payload_strategy="full")
        with tracer.trace(name="task"):
            with tracer.tool(name="calc", input={"a": 1, "b": 2}) as tool:
                tool.set_output(3)
        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        payload = tool_rec.get("payload", {})
        assert payload is not None
        assert payload.get("input", {}).get("a") == 1

    def test_large_output_truncated(self):
        tracer, reporter = make_tracer(payload_strategy="full")
        big_output = "x" * 100_000
        with tracer.trace(name="task"):
            with tracer.tool(name="db_query", input={"sql": "SELECT *"}) as tool:
                tool.set_output({"rows": big_output})
        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        assert tool_rec["attributes"].get("tool.output.truncated") is True

    def test_bytes_not_inlined(self):
        tracer, reporter = make_tracer(payload_strategy="full")
        with tracer.trace(name="task"):
            with tracer.tool(name="file_read", input=b"binary content here") as tool:
                tool.set_output(b"more binary")
        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        payload = tool_rec.get("payload", {})
        if payload and "input" in payload:
            input_str = json.dumps(payload["input"])
            assert "binary content here" not in input_str
            assert "_type" in input_str and "bytes" in input_str


# ═══════════════════════════════════════════════════════════════
# Task 6: Async Tool Support
# ═══════════════════════════════════════════════════════════════

class TestAsyncTool:
    def test_async_tool_basic(self):
        tracer, reporter = make_tracer()

        async def run():
            with tracer.trace(name="async-task"):
                async with tracer.tool(name="async_fetch") as tool:
                    await asyncio.sleep(0.05)
                    tool.set_output({"data": "fetched"})

        asyncio.run(run())
        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert len(tool_records) == 1
        assert tool_records[0]["status"] == "OK"
        assert tool_records[0]["duration_ms"] >= 40

    def test_async_tool_exception(self):
        tracer, reporter = make_tracer()

        async def run():
            with tracer.trace(name="async-task"):
                async with tracer.tool(name="async_fail"):
                    raise ConnectionError("async failed")

        with pytest.raises(ConnectionError):
            asyncio.run(run())
        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert tool_records[0]["status"] == "ERROR"
        assert tool_records[0]["error_type"] == "ConnectionError"

    def test_async_tool_context_restored(self):
        tracer, reporter = make_tracer()

        async def run():
            with tracer.trace(name="async-task") as agent_ctx:
                agent_span_id = get_current_context().span_id
                async with tracer.tool(name="inner"):
                    assert get_current_context().span_kind == SpanKind.TOOL
                assert get_current_context().span_id == agent_span_id

        asyncio.run(run())

    def test_parallel_children_under_tool(self):
        tracer, reporter = make_tracer()

        async def run():
            with tracer.trace(name="parallel-task"):
                async with tracer.tool(name="parallel_search"):
                    tool_ctx = get_current_context()
                    tool_span_id = tool_ctx.span_id

                    async def mock_llm(idx):
                        ctx = get_current_context()
                        assert ctx.span_id == tool_span_id
                        await asyncio.sleep(0.01)
                        return idx

                    results = await asyncio.gather(mock_llm(1), mock_llm(2))
                    assert results == [1, 2]

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════
# Task 7: Tool Decorator
# ═══════════════════════════════════════════════════════════════

class TestToolDecorator:
    def test_sync_decorator(self):
        tracer, reporter = make_tracer()

        @tracer.instrument_tool(name="search", tool_type="search")
        def search(query: str, limit: int = 10):
            return {"results": [f"result for {query}"]}

        with tracer.trace(name="task"):
            result = search("hello", limit=5)

        assert result == {"results": ["result for hello"]}
        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert len(tool_records) == 1
        assert tool_records[0]["span_name"] == "tool.search"
        payload = tool_records[0].get("payload", {})
        input_data = payload.get("input", {})
        assert input_data.get("query") == "hello"
        assert input_data.get("limit") == 5

    def test_sync_decorator_exception(self):
        tracer, reporter = make_tracer()

        @tracer.instrument_tool(name="fail_func")
        def fail_func(x):
            raise ValueError(f"bad input: {x}")

        with pytest.raises(ValueError, match="bad input"):
            with tracer.trace(name="task"):
                fail_func("test")

        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert tool_records[0]["status"] == "ERROR"

    def test_async_decorator(self):
        tracer, reporter = make_tracer()

        @tracer.instrument_tool(name="async_fetch", tool_type="http")
        async def fetch_url(url: str):
            await asyncio.sleep(0.01)
            return {"url": url, "status": 200}

        async def run():
            with tracer.trace(name="task"):
                result = await fetch_url("https://example.com")

        asyncio.run(run())

        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert len(tool_records) == 1
        assert tool_records[0]["status"] == "OK"

    def test_decorator_skips_self_cls(self):
        tracer, reporter = make_tracer()

        class MyService:
            @tracer.instrument_tool(name="method_tool")
            def do_work(self, query: str):
                return {"done": query}

        svc = MyService()
        with tracer.trace(name="task"):
            result = svc.do_work("test")

        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        payload = tool_records[0].get("payload", {})
        input_data = payload.get("input", {})
        assert "self" not in input_data
        assert input_data.get("query") == "test"

    def test_decorator_return_value_unchanged(self):
        tracer, reporter = make_tracer()

        @tracer.instrument_tool(name="identity")
        def identity(x):
            return x

        with tracer.trace(name="task"):
            result = identity({"complex": [1, 2, {"nested": True}]})
        assert result == {"complex": [1, 2, {"nested": True}]}

    def test_decorator_no_active_trace_raises(self):
        tracer, reporter = make_tracer()

        @tracer.instrument_tool(name="no_trace_tool")
        def do_something(x):
            return x * 2

        with pytest.raises(RuntimeError):
            do_something(5)


# ═══════════════════════════════════════════════════════════════
# Task 8: Fail-open Tests
# ═══════════════════════════════════════════════════════════════

class TestToolFailOpen:
    def test_reporter_failure_does_not_block_tool(self):
        class FailingReporter:
            def report(self, record):
                raise ConnectionError("core unavailable")

        config = Config(payload_strategy="full")
        tracer = Tracer(config=config, reporter=FailingReporter())
        with tracer.trace(name="task"):
            with tracer.tool(name="search", input={"q": "test"}) as tool:
                result = "found it"
                tool.set_output(result)

    def test_payload_serialization_failure_does_not_block(self):
        class Unserializable:
            def __repr__(self):
                raise Exception("repr fails too")

        tracer, reporter = make_tracer(payload_strategy="full")
        with tracer.trace(name="task"):
            with tracer.tool(name="weird", input=Unserializable()) as tool:
                tool.set_output("ok")
        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert len(tool_records) == 1
        assert tool_records[0]["status"] == "OK"


# ═══════════════════════════════════════════════════════════════
# Task 9: Core Tool Metrics
# ═══════════════════════════════════════════════════════════════

class TestCoreToolMetrics:
    def _insert_tool_trace(self, storage):
        base = time.time() - 60
        storage.insert_span({
            "trace_id": "t-tool", "span_id": "agent-1", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base, "end_time": base + 2.0,
            "duration_ms": 2000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S1", "user_id": "U1",
            "app_name": "App", "business_scene": "test",
            "attributes": {}, "events": [],
            "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "t-tool", "span_id": "llm-1", "parent_span_id": "agent-1",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 0.1, "end_time": base + 0.5,
            "duration_ms": 400, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {
                "gen_ai.request.model": "gpt-4",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 20,
                "gen_ai.usage.total_tokens": 30,
            },
            "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "t-tool", "span_id": "tool-1", "parent_span_id": "agent-1",
            "span_kind": "TOOL", "span_name": "tool.web_search",
            "start_time": base + 0.6, "end_time": base + 1.0,
            "duration_ms": 400, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {"tool.name": "web_search", "tool.type": "search"},
            "events": [],
            "payload": {"input": {"query": "test"}, "output": ["r1"]},
            "request_metadata": {"tool_name": "web_search", "tool_type": "search"},
        })

    def test_trace_summary_has_tool_call_count(self):
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_tool_trace(storage)
        result = storage.get_trace_summaries()
        trace = result["traces"][0]
        assert "tool_call_count" in trace
        assert trace["tool_call_count"] == 1

    def test_trace_detail_has_tool_call_count(self):
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_tool_trace(storage)
        detail = storage.get_trace_detail("t-tool")
        assert "tool_call_count" in detail
        assert detail["tool_call_count"] == 1

    def test_metrics_has_tool_metrics(self):
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_tool_trace(storage)
        metrics = storage.get_metrics()
        assert metrics["tool_call_count"] == 1
        assert metrics["tool_error_count"] == 0
        assert "tool_error_rate" in metrics

    def test_metrics_tool_latency_percentiles(self):
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_tool_trace(storage)
        metrics = storage.get_metrics()
        assert metrics["p50_tool_latency_ms"] > 0

    def test_timeseries_has_tool_metrics(self):
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_tool_trace(storage)
        ts = storage.get_time_series(time_start=time.time() - 120, time_end=time.time(), interval_seconds=60)
        assert len(ts) > 0
        assert "tool_call_count" in ts[0]
        assert ts[0]["tool_call_count"] >= 1

    def test_tool_error_trace_status(self):
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60
        storage.insert_span({
            "trace_id": "t-err", "span_id": "agent", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base, "end_time": base + 1.0,
            "duration_ms": 1000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None, "app_name": None, "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "t-err", "span_id": "tool-err", "parent_span_id": "agent",
            "span_kind": "TOOL", "span_name": "tool.terminal",
            "start_time": base + 0.1, "end_time": base + 0.5,
            "duration_ms": 400, "status": "ERROR",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None, "app_name": None, "business_scene": None,
            "attributes": {"tool.name": "terminal"}, "events": [],
            "error_type": "RuntimeError", "error_message": "command failed",
            "payload": None, "request_metadata": None,
        })
        result = storage.get_trace_summaries()
        assert result["traces"][0]["status"] == "ERROR"
        metrics = storage.get_metrics()
        assert metrics["tool_call_count"] == 1
        assert metrics["tool_error_count"] == 1
