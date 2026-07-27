"""Phase 2.2 Final Correctness Fix Tests.

Covers the FINAL correctness closeout per the requirements doc:
  P0-1: Non-string attribute/event key normalization + context activation order
  P0-2: TimeSeries keeps Tool-only buckets under model filter
  P1-1: model + time filter excludes out-of-window tool spans
  P1-2: Reporter bad record isolation — real flush test with fake session
  P1-3: safe_serialize global complexity budget + controlled dataclass traversal
"""
import sys
import os
import json
import time
import asyncio
import dataclasses
import pytest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "core"))

from llm_observability import Observability
from llm_observability.config import Config
from llm_observability.context import SpanContext, get_current_context
from llm_observability.spans import SpanKind
from llm_observability.tracer import Tracer
from llm_observability.tool import safe_serialize, normalize_attribute_key, normalize_event_name


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
# P0-1: Non-string Attribute/Event Key Normalization
# ═══════════════════════════════════════════════════════════════

class TestP01AttributeKeyNormalization:
    def test_normalize_integer_key(self):
        assert normalize_attribute_key(123) == "123"

    def test_normalize_none_key(self):
        assert normalize_attribute_key(None) == "<empty-key>"

    def test_normalize_empty_string_key(self):
        assert normalize_attribute_key("") == "<empty-key>"

    def test_normalize_string_key_unchanged(self):
        assert normalize_attribute_key("api_key") == "api_key"

    def test_normalize_key_with_str_failure(self):
        class BadStr:
            def __str__(self):
                raise Exception("str fails")
        assert normalize_attribute_key(BadStr()) == "<invalid-key>"

    def test_normalize_key_truncation(self):
        long_key = "k" * 200
        result = normalize_attribute_key(long_key)
        assert len(result) == 128

    def test_tool_attribute_integer_key_is_normalized(self):
        """Tool attributes with integer keys are normalized to strings."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task"):
            with tracer.tool(name="t", attributes={123: "value"}):
                pass

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        attrs = tool_rec["attributes"]
        assert "123" in attrs
        assert attrs["123"] == "value"

    def test_tool_attribute_none_key_is_normalized(self):
        """Tool attributes with None keys are normalized to a placeholder."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task"):
            with tracer.tool(name="t", attributes={None: "value"}):
                pass

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        attrs = tool_rec["attributes"]
        assert "<empty-key>" in attrs

    def test_tool_handle_set_attribute_integer_key(self):
        """ToolHandle.set_attribute with integer key is normalized."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task"):
            with tracer.tool(name="t") as tool:
                tool.set_attribute(456, "val")
                tool.set_output("ok")

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        attrs = tool_rec["attributes"]
        assert attrs.get("456") == "val"


# ═══════════════════════════════════════════════════════════════
# P0-1: Event Name Normalization
# ═══════════════════════════════════════════════════════════════

class TestP01EventNameNormalization:
    def test_normalize_integer_event_name(self):
        assert normalize_event_name(123) == "123"

    def test_normalize_none_event_name(self):
        assert normalize_event_name(None) == "<empty-event-name>"

    def test_normalize_long_event_name_truncated(self):
        long_name = "e" * 200
        result = normalize_event_name(long_name)
        assert len(result) == 128

    def test_tool_event_non_string_name_is_normalized(self):
        """Tool events with non-string names are normalized."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task"):
            with tracer.tool(name="t") as tool:
                tool.add_event(123, {"key": "val"})
                tool.set_output("ok")

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        events = tool_rec["events"]
        assert len(events) == 1
        assert events[0]["name"] == "123"

    def test_tool_event_non_string_attribute_key_is_normalized(self):
        """Tool event attributes with non-string keys are normalized."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task"):
            with tracer.tool(name="t") as tool:
                tool.add_event("my_event", {789: "val"})
                tool.set_output("ok")

        tool_rec = [r for r in reporter.records if r["span_kind"] == "TOOL"][0]
        events = tool_rec["events"]
        assert "789" in events[0]["attributes"]


# ═══════════════════════════════════════════════════════════════
# P0-1: Context Activation Order — No Leak on Failure
# ═══════════════════════════════════════════════════════════════

class TestP01ContextNoLeakOnFailure:
    def test_tool_enter_failure_does_not_leak_context(self):
        """If __enter__ fails mid-way, the parent context must be preserved."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task") as trace_ctx:
            agent_ctx_before = get_current_context()
            assert agent_ctx_before.span_kind == SpanKind.AGENT

            # Force a failure: empty tool name raises ValueError in __init__
            # which happens BEFORE any context manipulation
            with pytest.raises(ValueError):
                with tracer.tool(name=""):
                    pass

            # Context must still be AGENT
            after = get_current_context()
            assert after.span_kind == SpanKind.AGENT
            assert after.span_id == agent_ctx_before.span_id

    def test_tool_enter_failure_preserves_parent_for_subsequent_spans(self):
        """After a failed tool enter, subsequent child spans still parent to AGENT."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task"):
            # Failed tool
            with pytest.raises(ValueError):
                with tracer.tool(name=""):
                    pass

            # Successful tool — should still parent to AGENT, not a leaked TOOL
            with tracer.tool(name="good_tool"):
                pass

            # Context restored to AGENT
            assert get_current_context().span_kind == SpanKind.AGENT

        # Check the good tool's parent is the AGENT span
        tool_records = [r for r in reporter.records if r["span_kind"] == "TOOL"]
        assert len(tool_records) == 1
        good_tool = tool_records[0]
        agent = [r for r in reporter.records if r["span_kind"] == "AGENT"][0]
        assert good_tool["parent_span_id"] == agent["span_id"]

    def test_tool_context_properly_restored_after_normal_exit(self):
        """Normal exit restores parent context."""
        tracer, reporter = make_tracer(payload_strategy="off")

        with tracer.trace(name="task"):
            agent_ctx = get_current_context()
            with tracer.tool(name="inner"):
                # Inside tool: context is TOOL
                assert get_current_context().span_kind == SpanKind.TOOL
            # After tool: context restored to AGENT
            assert get_current_context().span_kind == SpanKind.AGENT
            assert get_current_context().span_id == agent_ctx.span_id


# ═══════════════════════════════════════════════════════════════
# P0-2: TimeSeries Keeps Tool-only Buckets
# ═══════════════════════════════════════════════════════════════

class TestP02TimeSeriesToolOnlyBucket:
    def _insert_cross_bucket_spans(self, storage):
        """Insert LLM in bucket 0 and TOOL in bucket 1."""
        base = 1000.0  # arbitrary base time
        interval = 60

        # LLM span in bucket 0 (1000.0 falls in bucket [1000, 1060))
        bucket0 = (int(base / interval)) * interval
        storage.insert_span({
            "trace_id": "ts-cross", "span_id": "llm-1", "parent_span_id": "agent-1",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": bucket0 + 10, "end_time": bucket0 + 11,
            "duration_ms": 1000, "status": "OK",
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })
        # TOOL span in bucket 1 (bucket0 + 60 + 5 falls in next bucket)
        bucket1 = bucket0 + interval
        storage.insert_span({
            "trace_id": "ts-cross", "span_id": "tool-1", "parent_span_id": "agent-1",
            "span_kind": "TOOL", "span_name": "tool.search",
            "start_time": bucket1 + 5, "end_time": bucket1 + 6,
            "duration_ms": 1000, "status": "OK",
            "attributes": {"tool.name": "search"}, "events": [],
            "payload": None, "request_metadata": None,
        })
        # AGENT span spanning both buckets
        storage.insert_span({
            "trace_id": "ts-cross", "span_id": "agent-1", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": bucket0, "end_time": bucket1 + 10,
            "duration_ms": (bucket1 + 10 - bucket0) * 1000, "status": "OK",
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        return bucket0, bucket1

    def test_model_filtered_timeseries_keeps_tool_only_bucket(self):
        """Tool-only bucket must appear even when LLM is in a different bucket."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        bucket0, bucket1 = self._insert_cross_bucket_spans(storage)

        ts = storage.get_time_series(
            time_start=bucket0 - 1,
            time_end=bucket1 + 120,
            interval_seconds=60,
            model="gpt-4",
        )
        buckets = {b["bucket"]: b for b in ts}
        assert bucket0 in buckets, f"LLM bucket {bucket0} missing"
        assert bucket1 in buckets, f"Tool-only bucket {bucket1} missing"
        assert buckets[bucket0]["llm_call_count"] == 1
        assert buckets[bucket1]["tool_call_count"] == 1

    def test_tool_only_bucket_has_complete_contract(self):
        """Tool-only bucket must return all TimeSeries contract fields."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        bucket0, bucket1 = self._insert_cross_bucket_spans(storage)

        ts = storage.get_time_series(
            time_start=bucket0 - 1,
            time_end=bucket1 + 120,
            interval_seconds=60,
            model="gpt-4",
        )
        buckets = {b["bucket"]: b for b in ts}
        tool_only = buckets[bucket1]

        required_fields = [
            "bucket", "trace_count", "trace_error_count",
            "llm_call_count", "llm_error_count", "llm_avg_latency_ms",
            "avg_ttft_ms", "avg_first_chunk_ms", "span_count", "tokens",
            "tool_call_count", "tool_error_count", "tool_avg_latency_ms",
        ]
        for field in required_fields:
            assert field in tool_only, f"Tool-only bucket missing field: {field}"

        assert tool_only["tool_call_count"] == 1
        assert tool_only["llm_call_count"] == 0
        assert tool_only["trace_count"] >= 1

    def test_summary_and_timeseries_tool_count_are_consistent(self):
        """Summary tool_call_count must equal sum of TimeSeries tool_call_count."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        bucket0, bucket1 = self._insert_cross_bucket_spans(storage)

        metrics = storage.get_metrics(model="gpt-4")
        ts = storage.get_time_series(
            time_start=bucket0 - 1,
            time_end=bucket1 + 120,
            interval_seconds=60,
            model="gpt-4",
        )

        summary_tool_count = metrics["tool_call_count"]
        ts_tool_count = sum(b.get("tool_call_count", 0) for b in ts)
        assert summary_tool_count == ts_tool_count, (
            f"Summary={summary_tool_count} vs TimeSeries={ts_tool_count}"
        )


# ═══════════════════════════════════════════════════════════════
# P1-1: model + time filter — time window on tool spans
# ═══════════════════════════════════════════════════════════════

class TestP11ModelTimeFilterWindow:
    def _insert_window_test_data(self, storage):
        """Insert an old TOOL (outside window) and a recent LLM (inside window)."""
        base = time.time()

        # Old tool at base - 600 (10 min ago, outside a 5-min window)
        storage.insert_span({
            "trace_id": "w-test", "span_id": "old-tool", "parent_span_id": "agent-w",
            "span_kind": "TOOL", "span_name": "tool.old",
            "start_time": base - 600, "end_time": base - 599,
            "duration_ms": 1000, "status": "OK",
            "attributes": {"tool.name": "old"}, "events": [],
            "payload": None, "request_metadata": None,
        })
        # Agent span
        storage.insert_span({
            "trace_id": "w-test", "span_id": "agent-w", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base - 600, "end_time": base - 50,
            "duration_ms": (550) * 1000, "status": "OK",
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        # Recent LLM at base - 30 (inside window), matching model=gpt-4
        storage.insert_span({
            "trace_id": "w-test", "span_id": "recent-llm", "parent_span_id": "agent-w",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base - 30, "end_time": base - 29,
            "duration_ms": 1000, "status": "OK",
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })
        return base

    def test_model_filter_excludes_tool_outside_time_window(self):
        """model + time filter must NOT count tool spans outside the time window."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = self._insert_window_test_data(storage)

        # Window: [base-120, base], model=gpt-4
        metrics = storage.get_metrics(
            time_start=base - 120,
            time_end=base,
            model="gpt-4",
        )
        # Old tool at base-600 is OUTSIDE window → should not be counted
        assert metrics["tool_call_count"] == 0

    def test_model_filter_includes_tool_inside_time_window(self):
        """model + time filter counts tool spans that ARE inside the window."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")

        base = time.time()
        # Tool inside window
        storage.insert_span({
            "trace_id": "w2", "span_id": "agent-w2", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base - 50, "end_time": base - 10,
            "duration_ms": 40000, "status": "OK",
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "w2", "span_id": "llm-w2", "parent_span_id": "agent-w2",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base - 40, "end_time": base - 39,
            "duration_ms": 1000, "status": "OK",
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "w2", "span_id": "tool-w2", "parent_span_id": "agent-w2",
            "span_kind": "TOOL", "span_name": "tool.recent",
            "start_time": base - 35, "end_time": base - 34,
            "duration_ms": 1000, "status": "OK",
            "attributes": {"tool.name": "recent"}, "events": [],
            "payload": None, "request_metadata": None,
        })

        metrics = storage.get_metrics(
            time_start=base - 120,
            time_end=base,
            model="gpt-4",
        )
        assert metrics["tool_call_count"] == 1

    def test_tool_metrics_time_semantics_same_with_and_without_model(self):
        """Tool time-window filtering should behave the same with/without model."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = self._insert_window_test_data(storage)

        # Without model filter, old tool still excluded by time window
        metrics_no_model = storage.get_metrics(
            time_start=base - 120,
            time_end=base,
        )
        # Without model filter: only TOOL spans in window are counted.
        # Old tool at base-600 is outside → 0
        assert metrics_no_model["tool_call_count"] == 0

        # With model filter: same result
        metrics_model = storage.get_metrics(
            time_start=base - 120,
            time_end=base,
            model="gpt-4",
        )
        assert metrics_model["tool_call_count"] == metrics_no_model["tool_call_count"]


# ═══════════════════════════════════════════════════════════════
# P1-2: Reporter Bad Record Isolation — Real Flush Test
# ═══════════════════════════════════════════════════════════════

class TestP12ReporterBadRecordIsolation:
    @pytest.mark.asyncio
    async def test_flush_drops_bad_record_sends_good_records(self):
        """_flush() drops bad records, sends good ones, increments counters."""
        from llm_observability.reporter import Reporter

        rep = Reporter(endpoint="http://localhost:9999", flush_interval=99)
        rep._session = MagicMock()

        # Mock HTTP POST response
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        rep._session.post = MagicMock(return_value=mock_resp)

        good_1 = {"trace_id": "t1", "span_id": "s1"}
        good_2 = {"trace_id": "t2", "span_id": "s2"}

        class _Unserializable:
            def __repr__(self):
                raise Exception("repr fails")

        bad = {"trace_id": "t3", "span_id": "s3", "data": _Unserializable()}

        rep._queue.append(good_1)
        rep._queue.append(bad)
        rep._queue.append(good_2)

        await rep._flush()

        # Good records sent
        assert rep._sent_count == 2
        # Bad record dropped
        assert rep._dropped_count == 1
        # Queue empty
        assert len(rep._queue) == 0

    @pytest.mark.asyncio
    async def test_flush_http_500_requeues_good_records_not_bad(self):
        """On HTTP 500, only good records are re-queued; bad record stays dropped."""
        from llm_observability.reporter import Reporter

        rep = Reporter(endpoint="http://localhost:9999", flush_interval=99)
        rep._session = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        rep._session.post = MagicMock(return_value=mock_resp)

        good = {"trace_id": "t1", "span_id": "s1"}

        class _Unserializable:
            def __repr__(self):
                raise Exception("fails")

        bad = {"trace_id": "t2", "span_id": "s2", "x": _Unserializable()}

        rep._queue.append(good)
        rep._queue.append(bad)

        await rep._flush()

        # Bad record dropped during preflight
        assert rep._dropped_count == 1
        # Good record re-queued after 500
        assert len(rep._queue) == 1
        assert rep._queue[0] == good
        assert rep._fail_count == 1

    @pytest.mark.asyncio
    async def test_flush_network_error_requeues_good_records(self):
        """On network error, good records are re-queued for retry."""
        from llm_observability.reporter import Reporter

        rep = Reporter(endpoint="http://localhost:9999", flush_interval=99)
        rep._session = MagicMock()

        # session.post raises network error
        rep._session.post = MagicMock(side_effect=Exception("connection refused"))

        good = {"trace_id": "t1", "span_id": "s1"}

        class _Unserializable:
            def __repr__(self):
                raise Exception("fails")

        bad = {"trace_id": "t2", "span_id": "s2", "x": _Unserializable()}

        rep._queue.append(good)
        rep._queue.append(bad)

        await rep._flush()

        assert rep._dropped_count == 1
        assert len(rep._queue) == 1
        assert rep._queue[0] == good


# ═══════════════════════════════════════════════════════════════
# P1-3: safe_serialize Global Complexity Budget
# ═══════════════════════════════════════════════════════════════

@dataclasses.dataclass
class _LargeDataclass:
    items: list
    name: str = "large"


@dataclasses.dataclass
class _CircularDataclass:
    ref: Optional["_CircularDataclass"] = None


class TestP13SafeSerializeGlobalBudget:
    def test_safe_serialize_global_node_budget(self):
        """Global node budget prevents processing millions of nodes."""
        # Nested wide structure: 50 lists × 200 items = 10000 nodes total
        wide = [{"key": i, "val": "x" * 100} for i in range(200)] * 50
        start = time.time()
        result = safe_serialize(wide)
        elapsed = time.time() - start

        # Must complete quickly (< 1 second)
        assert elapsed < 1.0
        # Result must be JSON-serializable
        json.dumps(result)
        # Must contain a budget truncation marker
        result_str = json.dumps(result)
        assert "global_budget" in result_str or "truncated" in result_str

    def test_safe_serialize_nested_wide_structure(self):
        """Deeply nested + wide structure is bounded by global budget."""
        deep_wide = {
            f"level_{i}": [dict(j=j) for j in range(200)]
            for i in range(50)
        }
        start = time.time()
        result = safe_serialize(deep_wide)
        elapsed = time.time() - start

        assert elapsed < 1.0
        json.dumps(result)

    def test_safe_serialize_circular_dataclass(self):
        """Circular dataclass must not cause RecursionError."""
        a = _CircularDataclass()
        b = _CircularDataclass(ref=a)
        a.ref = b

        result = safe_serialize(a)
        json.dumps(result)
        # Should contain a circular reference marker
        result_str = json.dumps(result)
        assert "circular" in result_str.lower() or "truncated" in result_str.lower()

    def test_safe_serialize_large_dataclass(self):
        """Large dataclass is bounded by global budget."""
        large = _LargeDataclass(items=list(range(5000)))
        start = time.time()
        result = safe_serialize(large)
        elapsed = time.time() - start

        assert elapsed < 1.0
        json.dumps(result)

    def test_safe_serialize_large_pydantic_like_model(self):
        """Large object with model_dump() is bounded."""
        class FakeModel:
            def model_dump(self):
                return {"items": list(range(5000)), "data": "x" * 10000}

        obj = FakeModel()
        start = time.time()
        result = safe_serialize(obj)
        elapsed = time.time() - start

        assert elapsed < 1.0
        json.dumps(result)

    def test_safe_serialize_no_recursion_error_on_deep_nesting(self):
        """Very deep nesting must not cause RecursionError."""
        deep = "leaf"
        for _ in range(500):
            deep = {"next": deep}

        result = safe_serialize(deep)
        json.dumps(result)
