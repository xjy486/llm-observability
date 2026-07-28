#!/usr/bin/env python3
"""
E2E test for second-round P0/P1 fixes.
Tests against a fresh SQLite DB with real span records.
"""
import sys, os, json, time, tempfile
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "core"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "proxy"))

from storage.db import Storage
from config import ProxyConfig

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

print("=" * 60)
print("E2E Test: Second-Round P0/P1 Fixes")
print("=" * 60)

# ── Test DB ──
db_path = tempfile.mktemp(suffix=".db")
storage = Storage(db_path)

# ── Test 1: Schema has first_chunk_ms, not ttfc_ms ──
print("\n[P0-NEW-04] Schema Migration Test")
cols = {row[1] for row in storage._get_conn().execute("PRAGMA table_info(spans)").fetchall()}
check("first_chunk_ms column exists", "first_chunk_ms" in cols, f"cols={cols}")
check("ttfc_ms column does NOT exist", "ttfc_ms" not in cols, f"cols={cols}")

# Check metadata table
meta = storage._get_conn().execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
check("metadata.schema_version = 2", meta and meta[0] == "2", f"meta={meta}")

# ── Test 2: Insert span with first_chunk_ms ──
print("\n[P0-NEW-02] Timing Metrics Test")
base_time = time.time()
record_stream = {
    "trace_id": "trace-stream-1",
    "span_id": "span-1",
    "span_name": "llm.completion",
    "span_kind": "LLM",
    "start_time": base_time,
    "end_time": base_time + 2.5,
    "duration_ms": 2500.0,
    "status": "OK",
    "http_status": 200,
    "ttft_ms": 300.0,        # Time to first token
    "first_chunk_ms": 320.0,  # Time to first SSE chunk
    "session_id": "sess-1",
    "user_id": "user-1",
    "attributes": {
        "gen_ai.request.model": "gpt-4",
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 200,
        "gen_ai.usage.total_tokens": 300,
        "llm.stream": True,
    },
    "payload": {"request": {"model": "gpt-4"}, "response": {"choices": [{"message": {"content": "Hello"}}]}},
}
storage.insert_span(record_stream)
check("Streaming span inserted", True)

# Non-streaming span — ttft_ms and first_chunk_ms should be null
record_nonstream = dict(record_stream)
record_nonstream["span_id"] = "span-2"
record_nonstream["trace_id"] = "trace-nonstream-1"
record_nonstream["ttft_ms"] = None
record_nonstream["first_chunk_ms"] = None
record_nonstream["attributes"] = {
    "gen_ai.request.model": "gpt-4",
    "gen_ai.usage.input_tokens": 50,
    "gen_ai.usage.output_tokens": 80,
    "gen_ai.usage.total_tokens": 130,
    "llm.stream": False,
}
storage.insert_span(record_nonstream)
check("Non-streaming span inserted", True)

# ── Test 3: Metrics query returns trace_count, llm_call_count, span_count ──
print("\n[P0-NEW-01] Metrics Summary Test")
metrics = storage.get_metrics()
check("metrics has trace_count", "trace_count" in metrics, f"keys={list(metrics.keys())}")
check("metrics trace_count >= 1", metrics.get("trace_count", 0) >= 1, f"trace_count={metrics.get('trace_count')}")
check("metrics has llm_call_count", "llm_call_count" in metrics, f"keys={list(metrics.keys())}")
check("metrics llm_call_count == 2", metrics.get("llm_call_count") == 2, f"llm_call_count={metrics.get('llm_call_count')}")
check("metrics has span_count", "span_count" in metrics, f"keys={list(metrics.keys())}")
check("metrics span_count == 2", metrics.get("span_count") == 2, f"span_count={metrics.get('span_count')}")
check("metrics does NOT have total_requests", "total_requests" not in metrics)
check("metrics has avg_first_chunk_ms", "avg_first_chunk_ms" in metrics, f"keys={list(metrics.keys())}")
check("metrics avg_first_chunk_ms == 320.0", metrics.get("avg_first_chunk_ms") == 320.0, f"val={metrics.get('avg_first_chunk_ms')}")
check("metrics avg_ttft_ms == 300.0", metrics.get("avg_ttft_ms") == 300.0, f"val={metrics.get('avg_ttft_ms')}")
check("metrics does NOT have avg_ttfc_ms", "avg_ttfc_ms" not in metrics)

# ── Test 4: Trace filter — trace-level status (P0-NEW-03) ──
print("\n[P0-NEW-03] Trace Filter Test")
# Insert an ERROR trace
record_error = dict(record_stream)
record_error["span_id"] = "span-3"
record_error["trace_id"] = "trace-error-1"
record_error["status"] = "ERROR"
record_error["error_type"] = "timeout"
storage.insert_span(record_error)

# Filter by status=ERROR at trace level
result = storage.get_trace_summaries(status="ERROR")
check("Trace filter status=ERROR returns 1", len(result["traces"]) == 1, f'count={len(result["traces"])}')
check("Trace filter ERROR trace_id correct", result["traces"][0]["trace_id"] == "trace-error-1" if result["traces"] else False)

# Filter by status=OK
result_ok = storage.get_trace_summaries(status="OK")
trace_ids_ok = [t["trace_id"] for t in result_ok["traces"]]
check("Trace filter status=OK returns 2", len(result_ok["traces"]) == 2, f"trace_ids={trace_ids_ok}")
check("OK filter excludes error trace", "trace-error-1" not in trace_ids_ok)

# ── Test 5: Duration filter (P0-NEW-03) ──
print("\n[P0-NEW-03] Duration Filter Test")
result_dur = storage.get_trace_summaries(min_duration_ms=2000.0)
check("Duration filter min=2000ms returns stream trace", 
      any(t["trace_id"] == "trace-stream-1" for t in result_dur["traces"]),
      f"trace_ids={[t['trace_id'] for t in result_dur['traces']]}")

result_short = storage.get_trace_summaries(max_duration_ms=1000.0)
check("Duration filter max=1000ms returns no traces with 2500ms",
      all(t["trace_id"] != "trace-stream-1" for t in result_short["traces"]),
      f"trace_ids={[t['trace_id'] for t in result_short['traces']]}")

# ── Test 6: Pagination total (P1-NEW-02) ──
print("\n[P1-NEW-02] Pagination Test")
result_page1 = storage.get_trace_summaries(limit=2, offset=0)
check("Pagination total == 3", result_page1["total"] == 3, f'total={result_page1["total"]}')
check("Pagination limit=2 returns 2 traces", len(result_page1["traces"]) == 2, f'count={len(result_page1["traces"])}')

result_page2 = storage.get_trace_summaries(limit=2, offset=2)
check("Pagination offset=2 returns 1 trace", len(result_page2["traces"]) == 1, f'count={len(result_page2["traces"])}')

# ── Test 7: TimeSeries (P1-NEW-01) ──
print("\n[P1-NEW-01] TimeSeries Test")
ts = storage.get_time_series(
    time_start=base_time - 10,
    time_end=base_time + 10,
    interval_seconds=60,
)
check("TimeSeries returns data", len(ts) > 0, f"ts={ts}")
if ts:
    bucket = ts[0]
    check("TimeSeries has trace_count", "trace_count" in bucket, f"keys={list(bucket.keys())}")
    check("TimeSeries has llm_call_count", "llm_call_count" in bucket, f"keys={list(bucket.keys())}")
    check("TimeSeries has span_count", "span_count" in bucket, f"keys={list(bucket.keys())}")
    check("TimeSeries has trace_error_count", "trace_error_count" in bucket, f"keys={list(bucket.keys())}")
    check("TimeSeries llm_call_count == 3", bucket.get("llm_call_count") == 3, f"val={bucket.get('llm_call_count')}")
    check("TimeSeries trace_error_count == 1", bucket.get("trace_error_count") == 1, f"val={bucket.get('trace_error_count')}")

# ── Test 8: Trace detail ──
print("\n[General] Trace Detail Test")
detail = storage.get_trace_detail("trace-stream-1")
check("Trace detail found", detail is not None)
check("Trace detail has span_count", detail and "span_count" in detail)
check("Trace detail has llm_call_count", detail and "llm_call_count" in detail)

# ── Test 9: MASK_KEYS env var (P1-NEW-03) ──
print("\n[P1-NEW-03] MASK_KEYS Environment Variable Test")
os.environ["MASK_KEYS"] = "custom_secret,my_api_key"
cfg = ProxyConfig.from_env()
check("MASK_KEYS env adds custom_secret", "custom_secret" in cfg.mask_keys, f"keys={cfg.mask_keys}")
check("MASK_KEYS env adds my_api_key", "my_api_key" in cfg.mask_keys, f"keys={cfg.mask_keys}")
check("MASK_KEYS preserves defaults", "authorization" in cfg.mask_keys, f"keys={cfg.mask_keys}")
check("MASK_KEYS total > 14 (defaults + 2 extra)", len(cfg.mask_keys) == 16, f"count={len(cfg.mask_keys)}")
del os.environ["MASK_KEYS"]

# ── Test 10: Models list ──
print("\n[General] Models List Test")
models = storage.get_models_list()
check("Models list returns 1 model", len(models) == 1, f"models={models}")
check("Models list model is gpt-4", models[0]["model"] == "gpt-4" if models else False)
check("Models list has trace_count", "trace_count" in (models[0] if models else {}), f"keys={list(models[0].keys()) if models else 'N/A'}")

# Cleanup
os.unlink(db_path)

# ── Summary ──
print("\n" + "=" * 60)
print(f"Results: {PASS} passed, {FAIL} failed")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
