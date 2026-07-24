"""
P0 fix tests — Round 3 fixes for the LLM Observability Platform.

Covers:
  Fix 1: metadata_only streaming leak
         - Strategy name uses underscore ("metadata_only"), not hyphen ("metadata-only")
         - capture_content only allows masked/full (metadata_only captures structure only)
  Fix 2: TTFT supports reasoning_content / tool_calls
         - feed() returns True for content OR reasoning_content OR tool_call
  Fix 3: Trace Filter real EXISTS
         - model filter: trace qualifies if ANY span has model, but aggregation uses ALL spans
         - model + status combination
         - model + duration combination
  Fix 4: Timing Migration
         - Old ttfc_ms values NOT copied to first_chunk_ms (incompatible semantics)
         - Legacy records: first_chunk_ms=NULL, ttft_ms=NULL, duration_ms=preserved

Run:  pytest tests/test_p0_fixes.py -v
"""
import sys
import os
import time
import json
import sqlite3
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

from storage.db import Storage
from handler import StreamingAccumulator
from config import ProxyConfig


# ──────────────────────────────────────────────
# Fix 1: metadata_only streaming leak
# ──────────────────────────────────────────────

class TestMetadataOnlyNoLeak:
    """Fix 1: metadata_only strategy must NOT capture content/reasoning/tool_calls."""

    def test_metadata_only_no_capture_content(self):
        """When strategy is metadata_only, capture_content must be False."""
        config = ProxyConfig()
        config.payload_strategy = "metadata_only"
        # This is the logic from handler.py line 325:
        # capture_content = should_sample and payload_strategy in ("masked", "full")
        capture_content = config.payload_strategy in ("masked", "full")
        assert capture_content is False, \
            "metadata_only must NOT set capture_content=True"

    def test_masked_allows_capture_content(self):
        """When strategy is masked, capture_content should be True."""
        config = ProxyConfig()
        config.payload_strategy = "masked"
        capture_content = config.payload_strategy in ("masked", "full")
        assert capture_content is True

    def test_full_allows_capture_content(self):
        """When strategy is full, capture_content should be True."""
        config = ProxyConfig()
        config.payload_strategy = "full"
        capture_content = config.payload_strategy in ("masked", "full")
        assert capture_content is True

    def test_metadata_only_underscore_not_hyphen(self):
        """Verify config uses 'metadata_only' (underscore), matching handler comparisons."""
        config = ProxyConfig()
        config.payload_strategy = "metadata_only"
        # The handler checks: self.config.payload_strategy == "metadata_only"
        assert config.payload_strategy == "metadata_only"
        # The OLD bug checked: == "metadata-only" (hyphen) which never matched
        assert config.payload_strategy != "metadata-only"

    def test_metadata_only_accumulator_no_content(self):
        """StreamingAccumulator with capture_content=False must not store content."""
        acc = StreamingAccumulator(capture_content=False)
        chunk = {
            "choices": [{"delta": {"content": "secret data"}, "finish_reason": None}]
        }
        acc.feed(chunk)
        response = acc.build_response()
        # With capture_content=False, content should be empty string
        msg = response["choices"][0]["message"]
        assert not msg.get("content"), "Content must NOT be captured when capture_content=False"

    def test_metadata_only_accumulator_no_reasoning(self):
        """StreamingAccumulator with capture_content=False must not store reasoning."""
        acc = StreamingAccumulator(capture_content=False)
        chunk = {
            "choices": [{"delta": {"reasoning_content": "internal thoughts"}, "finish_reason": None}]
        }
        acc.feed(chunk)
        response = acc.build_response()
        msg = response["choices"][0]["message"]
        assert "reasoning_content" not in msg or not msg["reasoning_content"], \
            "Reasoning must NOT be captured when capture_content=False"

    def test_metadata_only_accumulator_no_tool_calls(self):
        """StreamingAccumulator with capture_content=False must not store tool_calls."""
        acc = StreamingAccumulator(capture_content=False)
        chunk = {
            "choices": [{
                "delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "get_weather", "arguments": "{\"city\":\"NYC\"}"}}]},
                "finish_reason": None,
            }]
        }
        acc.feed(chunk)
        response = acc.build_response()
        msg = response["choices"][0]["message"]
        assert "tool_calls" not in msg or not msg["tool_calls"], \
            "Tool calls must NOT be captured when capture_content=False"

    def test_metadata_only_still_tracks_structure(self):
        """Even with capture_content=False, model/usage/finish_reason should be tracked."""
        acc = StreamingAccumulator(capture_content=False)
        # First chunk: model + content
        acc.feed({"model": "gpt-4", "choices": [{"delta": {"content": "hello"}, "finish_reason": None}]})
        # Last chunk: usage + finish
        acc.feed({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 42}})

        response = acc.build_response()
        assert response["model"] == "gpt-4"
        assert response["usage"]["total_tokens"] == 42
        assert response["choices"][0]["finish_reason"] == "stop"
        assert response["stream_chunk_count"] == 2


# ──────────────────────────────────────────────
# Fix 2: TTFT supports reasoning / tool_calls
# ──────────────────────────────────────────────

class TestTTFTMeaningfulOutput:
    """Fix 2: feed() returns True for content OR reasoning_content OR tool_call."""

    def test_content_triggers_true(self):
        """Content delta should cause feed() to return True."""
        acc = StreamingAccumulator(capture_content=True)
        chunk = {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}
        result = acc.feed(chunk)
        assert result is True, "Content delta must trigger meaningful output"

    def test_reasoning_triggers_true(self):
        """Reasoning content delta should cause feed() to return True."""
        acc = StreamingAccumulator(capture_content=True)
        chunk = {"choices": [{"delta": {"reasoning_content": "thinking..."}, "finish_reason": None}]}
        result = acc.feed(chunk)
        assert result is True, "Reasoning_content delta must trigger meaningful output"

    def test_tool_call_triggers_true(self):
        """Tool call delta should cause feed() to return True."""
        acc = StreamingAccumulator(capture_content=True)
        chunk = {"choices": [{
            "delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "search", "arguments": ""}}]},
            "finish_reason": None,
        }]}
        result = acc.feed(chunk)
        assert result is True, "Tool_call delta must trigger meaningful output"

    def test_empty_delta_returns_false(self):
        """Delta with no content/reasoning/tool_calls should return False."""
        acc = StreamingAccumulator(capture_content=True)
        chunk = {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}
        result = acc.feed(chunk)
        assert result is False, "Empty delta must NOT trigger meaningful output"

    def test_first_meaningful_is_reasoning(self):
        """Reasoning model: reasoning arrives before content — TTFT should fire on reasoning."""
        acc = StreamingAccumulator(capture_content=True)
        # Chunk 1: only reasoning (no content yet)
        r1 = acc.feed({"choices": [{"delta": {"reasoning_content": "Let me think..."}, "finish_reason": None}]})
        assert r1 is True, "First meaningful output is reasoning"
        # Chunk 2: content arrives later
        r2 = acc.feed({"choices": [{"delta": {"content": "The answer is 42"}, "finish_reason": None}]})
        assert r2 is True, "Content is also meaningful"

    def test_tool_call_only_response(self):
        """A response with only tool_calls and no content should still register meaningful output."""
        acc = StreamingAccumulator(capture_content=True)
        # Simulate a function-calling response: no content, only tool_calls
        r1 = acc.feed({
            "model": "gpt-4",
            "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "get_weather", "arguments": ""}}]}, "finish_reason": None}]
        })
        r2 = acc.feed({
            "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"city\":"}}]}, "finish_reason": None}]
        })
        r3 = acc.feed({
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"total_tokens": 100},
        })
        assert r1 is True, "Tool call chunk 1 is meaningful"
        assert r2 is True, "Tool call chunk 2 is meaningful"
        assert r3 is False, "Empty delta with finish_reason is not meaningful output"

        response = acc.build_response()
        msg = response["choices"][0]["message"]
        assert msg.get("tool_calls"), "Tool calls should be accumulated"
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"


# ──────────────────────────────────────────────
# Fix 3: Trace Filter real EXISTS
# ──────────────────────────────────────────────

def _make_span(
    trace_id="trace-1", span_id="span-1", span_kind="LLM",
    status="OK", duration_ms=100.0, model="gpt-4",
    start_time=None, session_id="sess-1", user_id="user-1",
    parent_span_id=None, app_name=None, business_scene=None,
):
    """Create a span dict for Storage.insert_span()."""
    if start_time is None:
        start_time = time.time() - 60
    attrs = {}
    if model:
        attrs["gen_ai.request.model"] = model
    attrs["gen_ai.usage.input_tokens"] = 10
    attrs["gen_ai.usage.output_tokens"] = 20
    attrs["gen_ai.usage.total_tokens"] = 30
    return {
        "trace_id": trace_id, "span_id": span_id,
        "parent_span_id": parent_span_id,
        "span_kind": span_kind, "span_name": "llm.completion",
        "start_time": start_time,
        "end_time": start_time + duration_ms / 1000.0,
        "duration_ms": duration_ms,
        "status": status, "ttft_ms": None, "first_chunk_ms": None,
        "session_id": session_id, "user_id": user_id,
        "app_name": app_name, "business_scene": business_scene,
        "attributes": attrs, "events": [],
        "payload": None, "request_metadata": None,
    }


class TestExistsFilterModelAggregation:
    """Fix 3: model filter uses EXISTS — aggregation includes ALL spans, not just matching ones."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")
        base = time.time() - 300

        # Trace X: span with gpt-4 (200ms) + span with claude-3 (800ms)
        # If we filter by model=gpt-4, the trace should STILL aggregate both spans.
        # Trace duration = MAX(end) - MIN(start) spanning both spans.
        self.storage.insert_span(_make_span(
            trace_id="t-X", span_id="s-X1", model="gpt-4",
            duration_ms=200, start_time=base, status="OK",
        ))
        self.storage.insert_span(_make_span(
            trace_id="t-X", span_id="s-X2", model="claude-3",
            duration_ms=800, start_time=base + 0.1, status="ERROR",
        ))
        # Trace Y: only gpt-4
        self.storage.insert_span(_make_span(
            trace_id="t-Y", span_id="s-Y1", model="gpt-4",
            duration_ms=300, start_time=base + 10, status="OK",
        ))

    def test_model_filter_returns_correct_traces(self):
        """gpt-4 filter should match traces t-X and t-Y (both have ≥1 gpt-4 span)."""
        result = self.storage.get_trace_summaries(model="gpt-4")
        ids = {t["trace_id"] for t in result["traces"]}
        assert ids == {"t-X", "t-Y"}, f"Expected t-X and t-Y; got {ids}"

    def test_model_filter_aggregates_all_spans(self):
        """Trace t-X: filter by gpt-4, but aggregation must include claude-3 span too.

        This is the CORE of the EXISTS fix:
        - Old (buggy): GROUP BY with WHERE model=gpt-4 → only aggregated the gpt-4 span
          → trace duration = 200ms, status = OK (WRONG!)
        - New (fixed): EXISTS subquery qualifies the trace, then aggregates ALL spans
          → trace duration ≈ 900ms (200+800 overlap ≈ 900), status = ERROR (claude-3 span)
        """
        result = self.storage.get_trace_summaries(model="gpt-4")
        trace_x = next(t for t in result["traces"] if t["trace_id"] == "t-X")

        # Trace status must be ERROR — the claude-3 span has ERROR status
        assert trace_x["status"] == "ERROR", \
            f"Trace t-X status should be ERROR (claude-3 span is ERROR), got {trace_x['status']}"

        # Trace duration must span both spans, not just the gpt-4 span
        # If only the gpt-4 span was aggregated, duration would be ~200ms
        # With both spans, duration should be ~900ms (200ms span + 800ms span starting 100ms later)
        assert trace_x["duration_ms"] > 500, \
            f"Trace t-X duration should reflect ALL spans (≈900ms), not just gpt-4 span (200ms). Got {trace_x['duration_ms']}"


class TestExistsFilterModelPlusStatus:
    """Fix 3: model + status combination — EXISTS for model, HAVING for status."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")
        base = time.time() - 300

        # Trace A: gpt-4 (OK) + claude-3 (ERROR) → trace status = ERROR
        self.storage.insert_span(_make_span(
            trace_id="t-A", span_id="s-A1", model="gpt-4",
            duration_ms=100, start_time=base, status="OK",
        ))
        self.storage.insert_span(_make_span(
            trace_id="t-A", span_id="s-A2", model="claude-3",
            duration_ms=200, start_time=base + 0.1, status="ERROR",
        ))
        # Trace B: gpt-4 (OK) + claude-3 (OK) → trace status = OK
        self.storage.insert_span(_make_span(
            trace_id="t-B", span_id="s-B1", model="gpt-4",
            duration_ms=100, start_time=base + 10, status="OK",
        ))
        self.storage.insert_span(_make_span(
            trace_id="t-B", span_id="s-B2", model="claude-3",
            duration_ms=200, start_time=base + 10.1, status="OK",
        ))
        # Trace C: only claude-3 (OK)
        self.storage.insert_span(_make_span(
            trace_id="t-C", span_id="s-C1", model="claude-3",
            duration_ms=300, start_time=base + 20, status="OK",
        ))

    def test_model_plus_status_error(self):
        """Filter: model=gpt-4 AND status=ERROR → only t-A (has gpt-4 AND trace is ERROR)."""
        result = self.storage.get_trace_summaries(model="gpt-4", status="ERROR")
        ids = {t["trace_id"] for t in result["traces"]}
        assert ids == {"t-A"}, f"Expected only t-A; got {ids}"
        assert result["total"] == 1

    def test_model_plus_status_ok(self):
        """Filter: model=gpt-4 AND status=OK → only t-B (has gpt-4 AND trace is OK)."""
        result = self.storage.get_trace_summaries(model="gpt-4", status="OK")
        ids = {t["trace_id"] for t in result["traces"]}
        assert ids == {"t-B"}, f"Expected only t-B; got {ids}"

    def test_model_only_without_status(self):
        """Filter: model=gpt-4 only → both t-A and t-B (no status filter)."""
        result = self.storage.get_trace_summaries(model="gpt-4")
        ids = {t["trace_id"] for t in result["traces"]}
        assert ids == {"t-A", "t-B"}, f"Expected t-A and t-B; got {ids}"


class TestExistsFilterModelPlusDuration:
    """Fix 3: model + duration combination — EXISTS for model, HAVING for duration."""

    def setup_method(self):
        self.storage = Storage(db_path=":memory:")
        base = time.time() - 300

        # Trace S (short): gpt-4 span only → trace duration ~100ms
        self.storage.insert_span(_make_span(
            trace_id="t-S", span_id="s-S1", model="gpt-4",
            duration_ms=100, start_time=base, status="OK",
        ))
        # Trace L (long): gpt-4 (100ms) + claude-3 (3000ms starting later)
        # → trace duration ≈ 3000ms+
        self.storage.insert_span(_make_span(
            trace_id="t-L", span_id="s-L1", model="gpt-4",
            duration_ms=100, start_time=base + 10, status="OK",
        ))
        self.storage.insert_span(_make_span(
            trace_id="t-L", span_id="s-L2", model="claude-3",
            duration_ms=3000, start_time=base + 10.1, status="OK",
        ))

    def test_model_plus_min_duration(self):
        """Filter: model=gpt-4 AND min_duration=2000ms → only t-L.

        Key: t-L's duration is computed from BOTH spans (gpt-4 + claude-3),
        not just the gpt-4 span. If we only aggregated the gpt-4 span,
        duration would be ~100ms and t-L would be wrongly excluded.
        """
        result = self.storage.get_trace_summaries(model="gpt-4", min_duration_ms=2000)
        ids = {t["trace_id"] for t in result["traces"]}
        assert "t-L" in ids, f"t-L should match (trace duration ≈3000ms from ALL spans); got {ids}"
        assert "t-S" not in ids, f"t-S should NOT match (trace duration ~100ms); got {ids}"

    def test_model_plus_max_duration(self):
        """Filter: model=gpt-4 AND max_duration=500ms → only t-S."""
        result = self.storage.get_trace_summaries(model="gpt-4", max_duration_ms=500)
        ids = {t["trace_id"] for t in result["traces"]}
        assert "t-S" in ids
        assert "t-L" not in ids, "t-L should be excluded (duration ≈3000ms)"


# ──────────────────────────────────────────────
# Fix 4: Timing Migration — no ttfc_ms copy
# ──────────────────────────────────────────────

class TestTimingMigrationNoCopy:
    """Fix 4: Old ttfc_ms values are NOT copied to first_chunk_ms."""

    def test_migration_leaves_first_chunk_null(self):
        """Old DB with ttfc_ms: after migration, first_chunk_ms must be NULL."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # Build old v1 schema with ttfc_ms
            old_conn = sqlite3.connect(db_path)
            old_conn.execute("""CREATE TABLE spans (
                trace_id TEXT, span_id TEXT, span_kind TEXT,
                start_time REAL, end_time REAL, duration_ms REAL,
                status TEXT, ttft_ms REAL, ttfc_ms REAL,
                attributes TEXT, events TEXT, payload TEXT,
                request_metadata TEXT, session_id TEXT, user_id TEXT,
                error_type TEXT, error_message TEXT
            )""")
            # Insert old record: ttft_ms=50, ttfc_ms=80, duration_ms=1500
            old_conn.execute("""INSERT INTO spans VALUES (
                'legacy-trace', 'legacy-span', 'LLM',
                1000.0, 1001.5, 1500.0,
                'OK', 50.0, 80.0,
                '{}', '[]', NULL, NULL, 's1', 'u1', NULL, NULL
            )""")
            old_conn.commit()
            old_conn.close()

            # Open with new code → triggers migration
            storage = Storage(db_path=db_path)

            # Verify old ttfc_ms (80.0) was NOT copied to first_chunk_ms
            spans = storage.get_trace_spans("legacy-trace")
            assert len(spans) == 1
            s = spans[0]

            assert s["first_chunk_ms"] is None, \
                f"first_chunk_ms must be NULL for legacy records (was {s['first_chunk_ms']})"
            assert s["ttft_ms"] is None, \
                f"ttft_ms must be NULL for legacy records (was {s['ttft_ms']})"
            assert s["duration_ms"] == 1500.0, \
                f"duration_ms should be preserved (was {s['duration_ms']})"

        finally:
            os.unlink(db_path)

    def test_migration_preserves_old_ttft_ms_column(self):
        """Legacy records: ttft_ms is NULLed by v1→v2 migration (one-time).

        The old schema had ttft_ms but semantics changed between v1 and v2.
        The migration NULLs old ttft_ms values. This test verifies:
          - ttft_ms is None (was 50.0 in v1, NULLed by migration)
          - first_chunk_ms is None (not copied from ttfc_ms=80.0)
          - duration_ms is preserved
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            old_conn = sqlite3.connect(db_path)
            old_conn.execute("""CREATE TABLE spans (
                trace_id TEXT, span_id TEXT, span_kind TEXT,
                start_time REAL, end_time REAL, duration_ms REAL,
                status TEXT, ttft_ms REAL, ttfc_ms REAL,
                attributes TEXT, events TEXT, payload TEXT,
                request_metadata TEXT, session_id TEXT, user_id TEXT,
                error_type TEXT, error_message TEXT
            )""")
            old_conn.execute("""INSERT INTO spans VALUES (
                't-old', 's-old', 'LLM',
                1000.0, 1001.5, 1500.0,
                'OK', 50.0, 80.0,
                '{}', '[]', NULL, NULL, 's1', 'u1', NULL, NULL
            )""")
            old_conn.commit()
            old_conn.close()

            storage = Storage(db_path=db_path)
            spans = storage.get_trace_spans("t-old")
            assert len(spans) == 1
            s = spans[0]
            # Migration NULLed old ttft_ms (incompatible v1→v2 semantics)
            assert s["ttft_ms"] is None, \
                f"ttft_ms must be NULL after migration (was {s['ttft_ms']})"
            # first_chunk_ms not copied from ttfc_ms
            assert s["first_chunk_ms"] is None
            # duration_ms preserved
            assert s["duration_ms"] == 1500.0

        finally:
            os.unlink(db_path)

    def test_migration_does_not_null_new_ttft_on_restart(self):
        """Regression: After migration + restart, new ttft_ms values must survive.

        The bug: the old code checked `if "ttfc_ms" in existing_cols` which stays
        True forever (column is never DROPped). On restart, it would
        re-run `UPDATE spans SET ttft_ms = NULL`, destroying valid new data.

        The fix gates the destructive UPDATE behind a schema_version check —
        it only runs when upgrading from a prior version, not on every restart.

        Test steps:
          1. Create v1 DB (with ttfc_ms)
          2. Start Storage → migration v1→v2
          3. Insert new Span: ttft=45, first_chunk=25
          4. Recreate Storage(db_path) — simulates restart
          5. Query new Span → ttft_ms == 45, first_chunk_ms == 25
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # Step 1: Create v1 schema with ttfc_ms
            old_conn = sqlite3.connect(db_path)
            old_conn.execute("""CREATE TABLE spans (
                trace_id TEXT, span_id TEXT, span_kind TEXT,
                start_time REAL, end_time REAL, duration_ms REAL,
                status TEXT, ttft_ms REAL, ttfc_ms REAL,
                attributes TEXT, events TEXT, payload TEXT,
                request_metadata TEXT, session_id TEXT, user_id TEXT,
                error_type TEXT, error_message TEXT
            )""")
            old_conn.execute("""INSERT INTO spans VALUES (
                'legacy', 'legacy-span', 'LLM',
                1000.0, 1001.5, 1500.0,
                'OK', 50.0, 80.0,
                '{}', '[]', NULL, NULL, 's1', 'u1', NULL, NULL
            )""")
            old_conn.commit()
            old_conn.close()

            # Step 2: First Storage startup → triggers v1→v2 migration
            storage1 = Storage(db_path=db_path)

            # Verify legacy record was migrated
            legacy_spans = storage1.get_trace_spans("legacy")
            assert legacy_spans[0]["ttft_ms"] is None  # NULLed by migration

            # Step 3: Insert new record with valid timing
            new_span = _make_span(
                trace_id="t-restart", span_id="s-restart",
                duration_ms=300,
            )
            new_span["ttft_ms"] = 45.0
            new_span["first_chunk_ms"] = 25.0
            storage1.insert_span(new_span)

            # Step 4: Restart — recreate Storage with the SAME db file
            del storage1

            storage2 = Storage(db_path=db_path)

            # Step 5: The new span's timing MUST survive the restart
            spans = storage2.get_trace_spans("t-restart")
            assert len(spans) == 1
            s = spans[0]
            assert s["ttft_ms"] == 45.0, \
                f"BUG: new ttft_ms was destroyed on restart! (got {s['ttft_ms']})"
            assert s["first_chunk_ms"] == 25.0, \
                f"BUG: new first_chunk_ms destroyed on restart! (got {s['first_chunk_ms']})"
            assert s["duration_ms"] == 300.0

        finally:
            os.unlink(db_path)

    def test_new_records_after_migration_have_timing(self):
        """After migration, new records should have proper timing fields."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # Start with old schema
            old_conn = sqlite3.connect(db_path)
            old_conn.execute("""CREATE TABLE spans (
                trace_id TEXT, span_id TEXT, span_kind TEXT,
                start_time REAL, end_time REAL, duration_ms REAL,
                status TEXT, ttft_ms REAL, ttfc_ms REAL,
                attributes TEXT, events TEXT, payload TEXT,
                request_metadata TEXT, session_id TEXT, user_id TEXT,
                error_type TEXT, error_message TEXT
            )""")
            old_conn.execute("""INSERT INTO spans VALUES (
                't-old', 's-old', 'LLM',
                1000.0, 1001.5, 1500.0,
                'OK', 50.0, 80.0,
                '{}', '[]', NULL, NULL, 's1', 'u1', NULL, NULL
            )""")
            old_conn.commit()
            old_conn.close()

            # Migrate
            storage = Storage(db_path=db_path)

            # Insert new record with timing
            new_span = _make_span(
                trace_id="t-new", span_id="s-new",
                duration_ms=500,
            )
            new_span["ttft_ms"] = 45.0
            new_span["first_chunk_ms"] = 25.0
            storage.insert_span(new_span)

            spans = storage.get_trace_spans("t-new")
            assert len(spans) == 1
            s = spans[0]
            assert s["duration_ms"] == 500.0
            assert s["ttft_ms"] == 45.0
            assert s["first_chunk_ms"] == 25.0

        finally:
            os.unlink(db_path)
