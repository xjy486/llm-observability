"""
Filter semantics tests — verify Trace filter behavior per P0-NEW-03 & P1-NEW-02.

Covers:
  11.3 Status filter     — trace-level aggregate (any span ERROR → trace ERROR)
  11.3 Duration filter   — trace-level aggregate (MAX(end) - MIN(start))
  11.3 Model filter      — span-level EXISTS (trace has ≥1 span with model)
  11.5 Pagination        — total count independent of page size; offset navigation
  11.5 Sorting           — sort by start_time, duration_ms, end_time
  11.5 Trace detail      — aggregate fields correct for multi-span traces

Run:  pytest tests/test_filter_semantics.py -v
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from storage.db import Storage


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def make_span(
    trace_id="trace-1",
    span_id="span-1",
    span_kind="LLM",
    status="OK",
    duration_ms=100.0,
    model="gpt-4",
    start_time=None,
    session_id="sess-1",
    user_id="user-1",
    parent_span_id=None,
    app_name=None,
    business_scene=None,
):
    """Create a span dict suitable for storage.insert_span()."""
    attrs = {}
    if model:
        attrs["gen_ai.request.model"] = model
    attrs["gen_ai.usage.input_tokens"] = 10
    attrs["gen_ai.usage.output_tokens"] = 20
    attrs["gen_ai.usage.total_tokens"] = 30

    if start_time is None:
        start_time = time.time() - 60

    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "span_kind": span_kind,
        "span_name": "llm.completion",
        "start_time": start_time,
        "end_time": start_time + duration_ms / 1000.0,
        "duration_ms": duration_ms,
        "status": status,
        "ttft_ms": None,
        "first_chunk_ms": None,
        "session_id": session_id,
        "user_id": user_id,
        "app_name": app_name,
        "business_scene": business_scene,
        "attributes": attrs,
        "events": [],
        "payload": None,
        "request_metadata": None,
    }


# ──────────────────────────────────────────────
# 11.3 Status filter — trace-level
# ──────────────────────────────────────────────

class TestStatusFilter:
    """P0-NEW-03: status filter is trace-level (any span ERROR → trace ERROR)."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")

    def test_status_filter_error(self):
        """Trace with mixed spans (one OK, one ERROR) should be ERROR trace."""
        self.storage.insert_span(make_span(trace_id="t-1", span_id="s-1a", status="OK"))
        self.storage.insert_span(make_span(trace_id="t-1", span_id="s-1b", status="ERROR"))
        self.storage.insert_span(make_span(trace_id="t-2", span_id="s-2", status="OK"))

        result = self.storage.get_trace_summaries(status="ERROR")
        assert result["total"] == 1
        assert result["traces"][0]["trace_id"] == "t-1"
        assert result["traces"][0]["status"] == "ERROR"

    def test_status_filter_ok(self):
        """Trace with all OK spans should be OK trace."""
        self.storage.insert_span(make_span(trace_id="t-1", span_id="s-1a", status="OK"))
        self.storage.insert_span(make_span(trace_id="t-1", span_id="s-1b", status="OK"))
        self.storage.insert_span(make_span(trace_id="t-2", span_id="s-2a", status="OK"))
        self.storage.insert_span(make_span(trace_id="t-2", span_id="s-2b", status="ERROR"))

        result = self.storage.get_trace_summaries(status="OK")
        # Only t-1 is fully OK; t-2 has an ERROR span
        assert result["total"] == 1
        assert result["traces"][0]["trace_id"] == "t-1"

    def test_status_filter_all(self):
        """No status filter returns all traces."""
        self.storage.insert_span(make_span(trace_id="t-1", span_id="s-1"))
        self.storage.insert_span(make_span(trace_id="t-2", span_id="s-2", status="ERROR"))
        result = self.storage.get_trace_summaries()
        assert result["total"] == 2


# ──────────────────────────────────────────────
# 11.3 Duration filter — trace-level
# ──────────────────────────────────────────────

class TestDurationFilter:
    """P0-NEW-03: min/max_duration_ms is trace-level (MAX(end) - MIN(start))."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")
        base = time.time() - 3600

        # Trace A: spans at t=base..base+1s → trace duration ≈ 1000ms
        self.storage.insert_span(make_span(
            trace_id="t-A", span_id="s-A1", duration_ms=100,
            start_time=base,
        ))
        self.storage.insert_span(make_span(
            trace_id="t-A", span_id="s-A2", duration_ms=200,
            start_time=base + 0.5,
        ))
        # Trace B: spans at t=base..base+5s → trace duration ≈ 5000ms
        self.storage.insert_span(make_span(
            trace_id="t-B", span_id="s-B1", duration_ms=300,
            start_time=base,
        ))
        self.storage.insert_span(make_span(
            trace_id="t-B", span_id="s-B2", duration_ms=500,
            start_time=base + 4.7,
        ))

    def test_min_duration_filter(self):
        """Only traces with trace-level duration >= 3000ms."""
        result = self.storage.get_trace_summaries(min_duration_ms=3000)
        ids = {t["trace_id"] for t in result["traces"]}
        assert "t-B" in ids, "t-B has ~5000ms trace duration"
        assert "t-A" not in ids, "t-A has ~1000ms trace duration"

    def test_max_duration_filter(self):
        """Only traces with trace-level duration <= 2000ms."""
        result = self.storage.get_trace_summaries(max_duration_ms=2000)
        ids = {t["trace_id"] for t in result["traces"]}
        assert "t-A" in ids
        assert "t-B" not in ids

    def test_duration_range_filter(self):
        """Combined min/max filter."""
        # t-A trace duration ≈ 700ms (base..base+0.7s)
        result = self.storage.get_trace_summaries(min_duration_ms=500, max_duration_ms=2000)
        ids = {t["trace_id"] for t in result["traces"]}
        assert ids == {"t-A"}


# ──────────────────────────────────────────────
# 11.3 Model filter — span-level EXISTS
# ──────────────────────────────────────────────

class TestModelFilter:
    """P0-NEW-03: model filter is span-level EXISTS (trace contains ≥1 span with model)."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")

        # Trace 1: has gpt-4 and claude-3 spans
        self.storage.insert_span(make_span(trace_id="t-1", span_id="s-1a", model="gpt-4"))
        self.storage.insert_span(make_span(trace_id="t-1", span_id="s-1b", model="claude-3"))
        # Trace 2: only gpt-4
        self.storage.insert_span(make_span(trace_id="t-2", span_id="s-2", model="gpt-4"))
        # Trace 3: only claude-3
        self.storage.insert_span(make_span(trace_id="t-3", span_id="s-3", model="claude-3"))

    def test_model_filter_gpt4(self):
        result = self.storage.get_trace_summaries(model="gpt-4")
        ids = {t["trace_id"] for t in result["traces"]}
        assert ids == {"t-1", "t-2"}, f"Expected t-1,t-2; got {result['traces']}"

    def test_model_filter_claude(self):
        result = self.storage.get_trace_summaries(model="claude-3")
        ids = {t["trace_id"] for t in result["traces"]}
        assert ids == {"t-1", "t-3"}

    def test_model_filter_nonexistent(self):
        result = self.storage.get_trace_summaries(model="nonexistent-model")
        assert result["total"] == 0


# ──────────────────────────────────────────────
# 11.5 Pagination
# ──────────────────────────────────────────────

class TestPagination:
    """P1-NEW-02: total count is independent of page; offset navigation works."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")
        now = time.time()
        for i in range(10):
            self.storage.insert_span(make_span(
                trace_id=f"trace-{i}",
                span_id=f"span-{i}",
                start_time=now - (10 - i),
            ))

    def test_total_independent_of_limit(self):
        """total should be the same regardless of limit."""
        r1 = self.storage.get_trace_summaries(limit=3)
        r2 = self.storage.get_trace_summaries(limit=5)
        r3 = self.storage.get_trace_summaries(limit=50)
        assert r1["total"] == r2["total"] == r3["total"] == 10

    def test_page_size_limit(self):
        """limit=N returns at most N traces."""
        result = self.storage.get_trace_summaries(limit=3)
        assert len(result["traces"]) == 3
        assert result["total"] == 10

    def test_offset_navigation(self):
        """offset=3 skips first 3 traces."""
        page1 = self.storage.get_trace_summaries(limit=5, offset=0, sort_by="start_time", sort_order="asc")
        page2 = self.storage.get_trace_summaries(limit=5, offset=5, sort_by="start_time", sort_order="asc")
        page1_ids = {t["trace_id"] for t in page1["traces"]}
        page2_ids = {t["trace_id"] for t in page2["traces"]}
        assert len(page1_ids) == 5
        assert len(page2_ids) == 5
        assert page1_ids.isdisjoint(page2_ids), "Pages must not overlap"

    def test_offset_beyond_data(self):
        """offset beyond total returns empty traces but correct total."""
        result = self.storage.get_trace_summaries(limit=5, offset=20)
        assert len(result["traces"]) == 0
        assert result["total"] == 10


# ──────────────────────────────────────────────
# 11.5 Sorting
# ──────────────────────────────────────────────

class TestSorting:
    """Sorting by start_time, duration_ms, end_time."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")
        base = time.time() - 100
        # Three traces with different start times and durations
        self.storage.insert_span(make_span(trace_id="t-1", span_id="s-1", start_time=base, duration_ms=500))
        self.storage.insert_span(make_span(trace_id="t-2", span_id="s-2", start_time=base + 10, duration_ms=100))
        self.storage.insert_span(make_span(trace_id="t-3", span_id="s-3", start_time=base + 5, duration_ms=300))

    def test_sort_start_time_asc(self):
        result = self.storage.get_trace_summaries(sort_by="start_time", sort_order="asc")
        starts = [t["start_time"] for t in result["traces"]]
        assert starts == sorted(starts), "start_time ASC"

    def test_sort_start_time_desc(self):
        result = self.storage.get_trace_summaries(sort_by="start_time", sort_order="desc")
        starts = [t["start_time"] for t in result["traces"]]
        assert starts == sorted(starts, reverse=True), "start_time DESC"

    def test_sort_duration_desc(self):
        result = self.storage.get_trace_summaries(sort_by="duration_ms", sort_order="desc")
        durations = [t["duration_ms"] for t in result["traces"]]
        assert durations == sorted(durations, reverse=True), "duration_ms DESC"


# ──────────────────────────────────────────────
# 11.5 Trace detail aggregation
# ──────────────────────────────────────────────

class TestTraceDetail:
    """get_trace_detail() must aggregate multi-span traces correctly."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")
        base = time.time() - 60
        # Root span (GATEWAY)
        self.storage.insert_span(make_span(
            trace_id="t-detail", span_id="root", span_kind="GATEWAY",
            duration_ms=1000, start_time=base, status="OK",
            model=None,
        ))
        # Child LLM span (streaming)
        self.storage.insert_span(make_span(
            trace_id="t-detail", span_id="child", span_kind="LLM",
            duration_ms=800, start_time=base + 0.1, status="ERROR",
            model="gpt-4", parent_span_id="root",
        ))

    def test_detail_aggregates_spans(self):
        detail = self.storage.get_trace_detail("t-detail")
        assert detail is not None
        assert detail["span_count"] == 2
        assert detail["llm_call_count"] == 1
        assert detail["status"] == "ERROR", "trace status = ERROR if any span ERROR"
        assert detail["trace_id"] == "t-detail"

    def test_detail_root_span(self):
        detail = self.storage.get_trace_detail("t-detail")
        assert detail["root_span_id"] == "root"

    def test_detail_not_found(self):
        detail = self.storage.get_trace_detail("nonexistent")
        assert detail is None

    def test_detail_tokens_aggregated_from_llm_only(self):
        detail = self.storage.get_trace_detail("t-detail")
        # Only the LLM span has tokens; GATEWAY span has None → treated as 0
        assert detail["input_tokens"] == 10  # from child span only
        assert detail["output_tokens"] == 20
        assert detail["total_tokens"] == 30
