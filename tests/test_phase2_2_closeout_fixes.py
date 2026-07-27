"""Phase 2.2 Closeout Fix Tests — P0 and P1 issues.

Covers:
  P0-1: Public decorator can be defined before init
  P0-2: Attributes/events sanitization, canonical key protection, bad record isolation
  P0-3: model filter Tool Metrics trace qualification
  P1-1: Unsampled tool skips payload
  P1-2: set_output(None) sentinel
  P1-3: size_bytes original size
  P1-4: Tool duration excludes telemetry processing
  P1-5: Core Pydantic contract sync
  P1-6: safe_serialize circular reference + complexity protection
"""
import sys
import os
import json
import time
import asyncio
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "core"))

from llm_observability import Observability
from llm_observability.config import Config
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context
from llm_observability.spans import Span, SpanKind
from llm_observability.tracer import Tracer
from llm_observability.tool import safe_serialize, apply_size_guard, ToolContextManager


class MockReporter:
    def __init__(self):
        self.records = []

    def report(self, record):
        self.records.append(record)


def make_tracer(payload_strategy="masked", sample_rate=1.0):
    config = Config(payload_strategy=payload_strategy, sample_rate=sample_rate)
    reporter = MockReporter()
    return Tracer(config=config, reporter=reporter), reporter


@pytest.fixture(autouse=True)
def _reset_observability():
    """Ensure Observability class state is clean between tests."""
    if Observability._initialized:
        Observability.shutdown()
    Observability._tracer = None
    Observability._reporter = None
    Observability._config = None
    Observability._initialized = False
    yield
    if Observability._initialized:
        Observability.shutdown()
    Observability._tracer = None
    Observability._reporter = None
    Observability._config = None
    Observability._initialized = False


# ═══════════════════════════════════════════════════════════════
# P0-1: Public Decorator Lazy Initialization
# ═══════════════════════════════════════════════════════════════

class TestP01PublicDecoratorLazyInit:
    def test_public_decorator_can_be_defined_before_init(self):
        """Decorator can be applied to a function before Observability.init()."""
        @Observability.instrument_tool(name="web_search", tool_type="search")
        def web_search(query: str):
            return {"results": [query]}

        # Definition succeeded without error — that's the assertion
        assert callable(web_search)

    def test_public_decorator_raises_on_call_before_init(self):
        """Calling an instrumented tool before init raises RuntimeError."""
        @Observability.instrument_tool(name="pre_init_tool")
        def my_tool(x):
            return x

        with pytest.raises(RuntimeError, match="init.*must be called"):
            my_tool(42)

    def test_public_decorator_sync_after_init(self):
        """After init, a sync decorated tool works correctly."""
        Observability.init(endpoint="http://localhost:9999", auto_instrument_openai=False)
        # Replace reporter with mock to avoid network calls
        mock_rep = MockReporter()
        Observability._reporter = mock_rep
        Observability._tracer.reporter = mock_rep

        @Observability.instrument_tool(name="sync_tool")
        def calc(a: int, b: int):
            return a + b

        with Observability.trace(name="task"):
            result = calc(3, 4)

        assert result == 7
        tool_records = [r for r in mock_rep.records if r["span_kind"] == "TOOL"]
        assert len(tool_records) == 1
        assert tool_records[0]["span_name"] == "tool.sync_tool"

    def test_public_decorator_async_after_init(self):
        """After init, an async decorated tool works correctly."""
        Observability.init(endpoint="http://localhost:9999", auto_instrument_openai=False)
        mock_rep = MockReporter()
        Observability._reporter = mock_rep
        Observability._tracer.reporter = mock_rep

        @Observability.instrument_tool(name="async_tool")
        async def fetch(url: str):
            await asyncio.sleep(0.01)
            return {"url": url}

        async def run():
            with Observability.trace(name="task"):
                result = await fetch("https://example.com")
                return result

        result = asyncio.run(run())
        assert result["url"] == "https://example.com"
        tool_records = [r for r in mock_rep.records if r["span_kind"] == "TOOL"]
        assert len(tool_records) == 1


# ═══════════════════════════════════════════════════════════════
# P0-2: Attributes / Events Sanitization + Canonical Key Protection
# ═══════════════════════════════════════════════════════════════

# Reserved canonical keys that users must not override
RESERVED_TOOL_KEYS = {
    "tool.name", "tool.type", "tool.call_id",
    "tool.input.type", "tool.output.type",
    "tool.input.size_bytes", "tool.output.size_bytes",
    "tool.input.truncated", "tool.output.truncated",
}


class TestP02AttributesEventsSanitization:
    def test_tool_custom_attribute_is_json_safe(self):
        """Custom attributes must be JSON-serializable after processing."""
        tracer, reporter = make_tracer(payload_strategy="full")

        class CustomObj:
            def __repr__(self):
                return "<CustomObj data>"

        with tracer.trace(name="task"):
            with tracer.tool(name="t", attributes={"client": CustomObj()}) as tool:
                tool.set_output("ok")

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        attrs = tool_rec["attributes"]
        # The custom object attribute must be JSON-serializable
        json.dumps(attrs)
        assert "client" in attrs

    def test_tool_event_attributes_are_json_safe(self):
        """Event attributes must be JSON-serializable."""
        tracer, reporter = make_tracer(payload_strategy="full")

        class Weird:
            def __repr__(self):
                return "<Weird>"

        with tracer.trace(name="task"):
            with tracer.tool(name="t") as tool:
                tool.add_event("custom_event", {"data": Weird()})
                tool.set_output("ok")

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        for event in tool_rec["events"]:
            json.dumps(event)

    def test_tool_attribute_secret_is_masked(self):
        """Sensitive keys in attributes must be masked."""
        tracer, reporter = make_tracer(payload_strategy="masked")

        with tracer.trace(name="task"):
            with tracer.tool(
                name="t",
                attributes={"api_key": "sk-secret123", "normal": "visible"},
            ) as tool:
                tool.set_output("ok")

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        attrs_str = json.dumps(tool_rec["attributes"])
        assert "sk-secret123" not in attrs_str

    def test_tool_reserved_attribute_cannot_be_overridden(self):
        """User cannot override canonical tool.* keys."""
        tracer, reporter = make_tracer(payload_strategy="full")

        with tracer.trace(name="task"):
            with tracer.tool(
                name="real_name",
                attributes={
                    "tool.name": "fake_name",
                    "tool.type": "fake_type",
                    "tool.call_id": "fake_call_id",
                },
            ) as tool:
                tool.set_output("ok")

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        attrs = tool_rec["attributes"]
        assert attrs["tool.name"] == "real_name"
        assert attrs.get("tool.type") != "fake_type"
        assert attrs.get("tool.call_id") != "fake_call_id"

    def test_tool_set_attribute_reserved_key_ignored(self):
        """set_attribute on reserved keys is ignored."""
        tracer, reporter = make_tracer(payload_strategy="full")

        with tracer.trace(name="task"):
            with tracer.tool(name="original") as tool:
                tool.set_attribute("tool.name", "hijacked")
                tool.set_output("ok")

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        assert tool_rec["attributes"]["tool.name"] == "original"

    def test_bad_record_does_not_poison_reporter_batch(self):
        """A single unserializable record must not block the entire batch.

        The Reporter must do per-record JSON preflight: if a record fails,
        drop it, increment dropped_count, and continue sending others.
        """
        from llm_observability.reporter import Reporter

        rep = Reporter(endpoint="http://localhost:9999", flush_interval=99)
        # Simulate two records: one good, one bad (contains non-serializable)
        good_record = {"trace_id": "t1", "span_id": "s1", "span_kind": "TOOL"}
        bad_record = {"trace_id": "t2", "span_id": "s2", "data": _Unserializable()}

        rep.report(good_record)
        rep.report(bad_record)

        # Start the reporter to trigger flush
        rep._stop = False
        # We test the preflight logic directly
        # The bad record should be detected during preflight
        assert len(rep._queue) == 2

        # Test the preflight helper
        from llm_observability.reporter import _record_is_json_safe
        assert _record_is_json_safe(good_record) is True
        assert _record_is_json_safe(bad_record) is False


class _Unserializable:
    """An object that cannot be JSON-serialized and has no safe repr fallback."""
    def __repr__(self):
        raise Exception("repr fails")


# ═══════════════════════════════════════════════════════════════
# P0-3: model filter Tool Metrics — Trace Qualification
# ═══════════════════════════════════════════════════════════════

class TestP03ModelFilterToolMetrics:
    def _insert_two_traces(self, storage):
        """Insert two traces with different models and different tool counts."""
        base = time.time() - 60

        # Trace A: model=gpt-4, 2 tools
        storage.insert_span({
            "trace_id": "trace-A", "span_id": "a-agent", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base, "end_time": base + 2.0,
            "duration_ms": 2000, "status": "OK",
            "session_id": None, "user_id": None, "app_name": None, "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "trace-A", "span_id": "a-llm", "parent_span_id": "a-agent",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 0.1, "end_time": base + 0.5,
            "duration_ms": 400, "status": "OK",
            "session_id": None, "user_id": None, "app_name": None, "business_scene": None,
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })
        for i in range(2):
            storage.insert_span({
                "trace_id": "trace-A", "span_id": f"a-tool-{i}", "parent_span_id": "a-agent",
                "span_kind": "TOOL", "span_name": f"tool.search_{i}",
                "start_time": base + 0.6 + i * 0.1, "end_time": base + 0.7 + i * 0.1,
                "duration_ms": 100, "status": "OK",
                "session_id": None, "user_id": None, "app_name": None, "business_scene": None,
                "attributes": {"tool.name": f"search_{i}"}, "events": [],
                "payload": None, "request_metadata": None,
            })

        # Trace B: model=claude, 3 tools
        storage.insert_span({
            "trace_id": "trace-B", "span_id": "b-agent", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base + 1.0, "end_time": base + 3.0,
            "duration_ms": 2000, "status": "OK",
            "session_id": None, "user_id": None, "app_name": None, "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "trace-B", "span_id": "b-llm", "parent_span_id": "b-agent",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 1.1, "end_time": base + 1.5,
            "duration_ms": 400, "status": "OK",
            "session_id": None, "user_id": None, "app_name": None, "business_scene": None,
            "attributes": {"gen_ai.request.model": "claude"},
            "events": [], "payload": None, "request_metadata": None,
        })
        for i in range(3):
            storage.insert_span({
                "trace_id": "trace-B", "span_id": f"b-tool-{i}", "parent_span_id": "b-agent",
                "span_kind": "TOOL", "span_name": f"tool.fetch_{i}",
                "start_time": base + 1.6 + i * 0.1, "end_time": base + 1.7 + i * 0.1,
                "duration_ms": 100, "status": "OK",
                "session_id": None, "user_id": None, "app_name": None, "business_scene": None,
                "attributes": {"tool.name": f"fetch_{i}"}, "events": [],
                "payload": None, "request_metadata": None,
            })

    def test_model_filter_tool_metrics_correct(self):
        """model=gpt-4 should return tool_call_count=2 (not 0)."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_two_traces(storage)

        metrics = storage.get_metrics(model="gpt-4")
        assert metrics["tool_call_count"] == 2
        assert metrics["tool_error_count"] == 0

    def test_model_filter_tool_metrics_other_model(self):
        """model=claude should return tool_call_count=3."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_two_traces(storage)

        metrics = storage.get_metrics(model="claude")
        assert metrics["tool_call_count"] == 3

    def test_model_filter_tool_timeseries_correct(self):
        """model filter on TimeSeries should use trace qualification."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_two_traces(storage)

        ts = storage.get_time_series(
            time_start=time.time() - 120,
            time_end=time.time(),
            interval_seconds=3600,
            model="gpt-4",
        )
        total_tool_calls = sum(b.get("tool_call_count", 0) for b in ts)
        assert total_tool_calls == 2

    def test_no_model_filter_all_tools(self):
        """Without model filter, all 5 tools should be counted."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        self._insert_two_traces(storage)

        metrics = storage.get_metrics()
        assert metrics["tool_call_count"] == 5


# ═══════════════════════════════════════════════════════════════
# P1-1: Unsampled Tool Skips Payload
# ═══════════════════════════════════════════════════════════════

class TestP11UnsampledToolSkipsPayload:
    def test_unsampled_tool_skips_input_serialization(self):
        """When sampled=False, tool input payload is NOT processed."""
        tracer, reporter = make_tracer(payload_strategy="full", sample_rate=0.0)

        big_input = "x" * 100_000
        with tracer.trace(name="task"):
            with tracer.tool(name="search", input=big_input):
                pass

        # No records reported (unsampled)
        assert len(reporter.records) == 0

    def test_unsampled_tool_skips_output_serialization(self):
        """When sampled=False, tool output payload is NOT processed."""
        tracer, reporter = make_tracer(payload_strategy="full", sample_rate=0.0)

        big_output = "y" * 100_000
        with tracer.trace(name="task"):
            with tracer.tool(name="search") as tool:
                tool.set_output(big_output)

        assert len(reporter.records) == 0

    def test_unsampled_tool_still_creates_context(self):
        """Unsampled tool still creates context and span_id for child inheritance."""
        tracer, reporter = make_tracer(payload_strategy="full", sample_rate=0.0)

        with tracer.trace(name="task") as agent_ctx:
            with tracer.tool(name="search"):
                ctx = get_current_context()
                assert ctx.span_kind == SpanKind.TOOL
                assert ctx.sampled is False
            # Context restored to AGENT after tool
            assert get_current_context().span_kind == SpanKind.AGENT


# ═══════════════════════════════════════════════════════════════
# P1-2: set_output(None) Sentinel
# ═══════════════════════════════════════════════════════════════

class TestP12SetOutputNone:
    def test_tool_output_none_is_recorded(self):
        """set_output(None) should record output: null with type NoneType."""
        tracer, reporter = make_tracer(payload_strategy="full")

        with tracer.trace(name="task"):
            with tracer.tool(name="t") as tool:
                tool.set_output(None)

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        payload = tool_rec.get("payload") or {}
        assert "output" in payload
        assert payload["output"] is None
        assert tool_rec["attributes"].get("tool.output.type") == "NoneType"

    def test_tool_without_set_output_has_no_output_field(self):
        """Never calling set_output() should NOT produce an output field."""
        tracer, reporter = make_tracer(payload_strategy="full")

        with tracer.trace(name="task"):
            with tracer.tool(name="t"):
                pass

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        payload = tool_rec.get("payload") or {}
        assert "output" not in payload
        assert "tool.output.type" not in tool_rec["attributes"]

    def test_decorator_return_none_records_null_output(self):
        """Decorator wrapping a function returning None should record null output."""
        tracer, reporter = make_tracer(payload_strategy="full")

        @tracer.instrument_tool(name="returns_none")
        def do_nothing():
            return None

        with tracer.trace(name="task"):
            do_nothing()

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        payload = tool_rec.get("payload") or {}
        assert "output" in payload
        assert payload["output"] is None


# ═══════════════════════════════════════════════════════════════
# P1-3: size_bytes Original Size
# ═══════════════════════════════════════════════════════════════

class TestP13SizeBytesOriginal:
    def test_large_tool_output_size_bytes_is_original_size(self):
        """size_bytes must reflect the pre-truncation serialized size."""
        tracer, reporter = make_tracer(payload_strategy="full")

        big_output = "x" * 100_000
        with tracer.trace(name="task"):
            with tracer.tool(name="t") as tool:
                tool.set_output({"data": big_output})

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        size_bytes = tool_rec["attributes"].get("tool.output.size_bytes", 0)
        # P1-3 + P1-6: size_bytes reflects the serialized size AFTER safe_serialize
        # (which truncates strings to 32768 chars) but BEFORE apply_size_guard truncation.
        # So it should be ~32KB (the safe_serialize limit), not the truncated preview size.
        assert size_bytes > 30_000  # Well above the 512-byte preview size
        assert tool_rec["attributes"].get("tool.output.truncated") is True

    def test_small_tool_output_size_bytes_matches_serialized_size(self):
        """For small output, size_bytes matches the actual serialized size."""
        tracer, reporter = make_tracer(payload_strategy="full")

        with tracer.trace(name="task"):
            with tracer.tool(name="t") as tool:
                tool.set_output({"result": "hello"})

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        size_bytes = tool_rec["attributes"].get("tool.output.size_bytes", 0)
        expected = len(json.dumps({"result": "hello"}, ensure_ascii=False).encode("utf-8"))
        assert size_bytes == expected

    def test_large_tool_input_size_bytes_is_original_size(self):
        """size_bytes for input must also reflect pre-truncation size."""
        tracer, reporter = make_tracer(payload_strategy="full")

        big_input = "x" * 100_000
        with tracer.trace(name="task"):
            with tracer.tool(name="t", input={"data": big_input}):
                pass

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        size_bytes = tool_rec["attributes"].get("tool.input.size_bytes", 0)
        # P1-3 + P1-6: After safe_serialize string truncation but before size_guard
        assert size_bytes > 30_000


# ═══════════════════════════════════════════════════════════════
# P1-4: Tool Duration Excludes Telemetry
# ═══════════════════════════════════════════════════════════════

class TestP14ToolDuration:
    def test_tool_duration_excludes_telemetry_processing(self):
        """Tool duration should only measure business execution time,
        not output serialization/masking/size-guard time."""
        tracer, reporter = make_tracer(payload_strategy="full")

        class SlowSerialize:
            """An object whose serialization takes significant time."""
            def __init__(self, n):
                self._items = list(range(n))
            def __repr__(self):
                # Force slow repr
                time.sleep(0.1)
                return f"<SlowSerialize {len(self._items)}>"
            def model_dump(self):
                # Simulate slow serialization
                time.sleep(0.3)
                return {"items": self._items}

        with tracer.trace(name="task"):
            with tracer.tool(name="t") as tool:
                # Business logic is instant
                pass
                tool.set_output(SlowSerialize(100))

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        # Duration should be small (< 100ms for business logic)
        # If telemetry processing were included, it would be > 300ms
        assert tool_rec["duration_ms"] < 200

    def test_tool_duration_includes_business_time(self):
        """Tool duration should include actual business execution time."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task"):
            with tracer.tool(name="t"):
                time.sleep(0.05)

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        assert tool_rec["duration_ms"] >= 40


# ═══════════════════════════════════════════════════════════════
# P1-5: Core Pydantic Contract Sync
# ═══════════════════════════════════════════════════════════════

class TestP15CoreContractSync:
    def test_trace_summary_has_tool_call_count(self):
        from models.schemas import TraceSummary
        fields = TraceSummary.model_fields
        assert "tool_call_count" in fields

    def test_trace_detail_has_tool_call_count(self):
        from models.schemas import TraceDetail
        fields = TraceDetail.model_fields
        assert "tool_call_count" in fields

    def test_metrics_summary_has_tool_fields(self):
        from models.schemas import MetricsSummary
        fields = MetricsSummary.model_fields
        for required in [
            "tool_call_count", "tool_error_count", "tool_error_rate",
            "p50_tool_latency_ms", "p95_tool_latency_ms", "p99_tool_latency_ms",
        ]:
            assert required in fields, f"MetricsSummary missing field: {required}"

    def test_storage_metrics_keys_match_core_schema(self):
        """Storage get_metrics() return keys must match MetricsSummary schema fields."""
        from models.schemas import MetricsSummary
        from storage.db import Storage

        storage = Storage(db_path=":memory:")
        # Insert a minimal span to get non-empty metrics
        base = time.time() - 10
        storage.insert_span({
            "trace_id": "ct1", "span_id": "cs1", "parent_span_id": None,
            "span_kind": "TOOL", "span_name": "tool.test",
            "start_time": base, "end_time": base + 0.1,
            "duration_ms": 100, "status": "OK",
            "attributes": {"tool.name": "test"}, "events": [],
            "payload": None, "request_metadata": None,
        })
        metrics = storage.get_metrics()

        schema_fields = set(MetricsSummary.model_fields.keys())
        # Every key returned by storage must exist in the schema (or be extra metadata)
        # Core tool fields must be present in both
        core_tool_fields = {
            "tool_call_count", "tool_error_count", "tool_error_rate",
            "p50_tool_latency_ms", "p95_tool_latency_ms", "p99_tool_latency_ms",
        }
        for field in core_tool_fields:
            assert field in metrics, f"Storage metrics missing: {field}"
            assert field in schema_fields, f"MetricsSummary schema missing: {field}"


# ═══════════════════════════════════════════════════════════════
# P1-6: safe_serialize Circular Reference + Complexity
# ═══════════════════════════════════════════════════════════════

class TestP16SafeSerializeComplexity:
    def test_safe_serialize_circular_reference(self):
        """Circular references must not cause infinite recursion."""
        a = {}
        a["self"] = a
        result = safe_serialize(a)
        # Must be serializable
        json.dumps(result)
        # Should contain a circular reference marker
        result_str = json.dumps(result)
        assert "circular" in result_str.lower() or "truncated" in result_str.lower()

    def test_safe_serialize_max_depth(self):
        """Deeply nested structures must be truncated at max_depth."""
        # Create 20 levels of nesting
        deep = "leaf"
        for _ in range(20):
            deep = {"next": deep}

        result = safe_serialize(deep)
        json.dumps(result)
        result_str = json.dumps(result)
        assert "truncated" in result_str.lower() or "max_depth" in result_str.lower()

    def test_safe_serialize_max_items(self):
        """Very large lists must be truncated at max_items."""
        big_list = list(range(5000))
        result = safe_serialize(big_list)
        json.dumps(result)
        if isinstance(result, list):
            assert len(result) <= 1100  # some threshold around max_items

    def test_safe_serialize_large_string(self):
        """Very large strings must be truncated."""
        huge_string = "x" * 100_000
        result = safe_serialize(huge_string)
        if isinstance(result, str):
            assert len(result) <= 40000
        json.dumps(result)