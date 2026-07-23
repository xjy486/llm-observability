"""
SQLite storage layer for the Observability Core.

Stores spans in a single table; traces are derived by aggregating spans
with the same trace_id. Supports incremental assembly for late-arriving spans.

P0-05: Metrics distinguish Trace / LLM Call / Span levels.
P0-06: Trace filtering uses two-phase query (find matching trace_ids, then aggregate all spans).
P0-07: Pagination returns correct total count.
P0-04: TTFT/TTFC semantics separated.
"""
import sqlite3
import json
import os
import threading
import logging
from typing import Optional
from collections import defaultdict

logger = logging.getLogger("core.storage")


class Storage:
    """Thread-safe SQLite storage."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local connection."""
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self):
        """Initialize database schema."""
        with self._init_lock:
            if self._initialized:
                return
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS spans (
                    trace_id TEXT NOT NULL,
                    span_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    span_name TEXT NOT NULL,
                    span_kind TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL NOT NULL,
                    duration_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    ttft_ms REAL,
                    ttfc_ms REAL,
                    session_id TEXT,
                    user_id TEXT,
                    app_name TEXT,
                    business_scene TEXT,
                    attributes TEXT,  -- JSON
                    events TEXT,      -- JSON
                    error_type TEXT,
                    error_message TEXT,
                    payload TEXT,     -- JSON
                    request_metadata TEXT,  -- JSON
                    payload_ref TEXT,
                    trace_inherited INTEGER DEFAULT 0,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    is_stream INTEGER,
                    created_at REAL DEFAULT (strftime('%s', 'now')),
                    PRIMARY KEY (trace_id, span_id)
                );

                CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
                CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time);
                CREATE INDEX IF NOT EXISTS idx_spans_status ON spans(status);
                CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id);
                CREATE INDEX IF NOT EXISTS idx_spans_user ON spans(user_id);
                CREATE INDEX IF NOT EXISTS idx_spans_model ON spans(model);
                CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(span_kind);
            """)
            conn.commit()
            self._initialized = True
            logger.info("Storage initialized: %s", self.db_path)

    def insert_span(self, record: dict) -> bool:
        """Insert or replace a span record."""
        conn = self._get_conn()

        # Extract model and tokens from attributes
        attrs = record.get("attributes", {})
        model = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model")
        input_tokens = attrs.get("gen_ai.usage.input_tokens")
        output_tokens = attrs.get("gen_ai.usage.output_tokens")
        total_tokens = attrs.get("gen_ai.usage.total_tokens")
        is_stream = attrs.get("llm.stream")

        conn.execute("""
            INSERT OR REPLACE INTO spans (
                trace_id, span_id, parent_span_id, span_name, span_kind,
                start_time, end_time, duration_ms, status, http_status,
                ttft_ms, ttfc_ms, session_id, user_id, app_name, business_scene,
                attributes, events, error_type, error_message, payload,
                request_metadata, payload_ref, trace_inherited,
                model, input_tokens, output_tokens, total_tokens, is_stream
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["trace_id"],
            record["span_id"],
            record.get("parent_span_id"),
            record.get("span_name", "llm.completion"),
            record.get("span_kind", "LLM"),
            record["start_time"],
            record["end_time"],
            record["duration_ms"],
            record.get("status", "OK"),
            record.get("http_status"),
            record.get("ttft_ms"),
            record.get("ttfc_ms"),
            record.get("session_id"),
            record.get("user_id"),
            record.get("app_name"),
            record.get("business_scene"),
            json.dumps(record.get("attributes", {})),
            json.dumps(record.get("events", [])),
            record.get("error_type"),
            record.get("error_message"),
            json.dumps(record.get("payload")) if record.get("payload") else None,
            json.dumps(record.get("request_metadata")) if record.get("request_metadata") else None,
            record.get("payload_ref"),
            1 if record.get("trace_inherited") else 0,
            model,
            input_tokens,
            output_tokens,
            total_tokens,
            1 if is_stream else (0 if is_stream is not None else None),
        ))
        conn.commit()
        return True

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a database row to a dict."""
        d = dict(row)
        d["attributes"] = json.loads(d["attributes"]) if d["attributes"] else {}
        d["events"] = json.loads(d["events"]) if d["events"] else []
        d["payload"] = json.loads(d["payload"]) if d["payload"] else None
        d["request_metadata"] = json.loads(d["request_metadata"]) if d["request_metadata"] else None
        d["trace_inherited"] = bool(d["trace_inherited"])
        if d["is_stream"] is not None:
            d["is_stream"] = bool(d["is_stream"])
        return d

    def get_trace_spans(self, trace_id: str) -> list[dict]:
        """Get all spans for a trace."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_time",
            (trace_id,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_trace_summaries(
        self,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        status: Optional[str] = None,
        model: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        min_duration_ms: Optional[float] = None,
        max_duration_ms: Optional[float] = None,
        app_name: Optional[str] = None,
        business_scene: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "start_time",
        sort_order: str = "desc",
    ) -> dict:
        """Get trace summaries with filtering.

        P0-06: Two-phase query — first find trace_ids that match filters,
        then aggregate ALL spans for those trace_ids (no partial traces).

        P0-07: Returns dict with 'traces' list and 'total' count for pagination.

        Returns:
            {"traces": [...], "total": int}
        """
        conn = self._get_conn()

        # ── Phase 1: Find matching trace_ids ──
        # Build WHERE for span-level filters
        conditions = []
        params = []

        if time_start is not None:
            conditions.append("start_time >= ?")
            params.append(time_start)
        if time_end is not None:
            conditions.append("start_time <= ?")
            params.append(time_end)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if model is not None:
            conditions.append("model = ?")
            params.append(model)
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if trace_id is not None:
            conditions.append("trace_id = ?")
            params.append(trace_id)
        if app_name is not None:
            conditions.append("app_name = ?")
            params.append(app_name)
        if business_scene is not None:
            conditions.append("business_scene = ?")
            params.append(business_scene)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Get distinct trace_ids that have at least one matching span
        matching_trace_ids_query = f"""
            SELECT DISTINCT trace_id FROM spans WHERE {where_clause}
        """
        matching_rows = conn.execute(matching_trace_ids_query, params).fetchall()
        matching_trace_ids = [r["trace_id"] for r in matching_rows]

        if not matching_trace_ids:
            return {"traces": [], "total": 0}

        total = len(matching_trace_ids)

        # ── Phase 2: Aggregate ALL spans for matching trace_ids ──
        # Use a CTE for trace_id list, then aggregate without span-level WHERE
        placeholders = ",".join("?" * len(matching_trace_ids))

        # Valid sort columns (map to trace-level aggregate expressions)
        trace_sort_map = {
            "start_time": "MIN(s.start_time)",
            "duration_ms": "(MAX(s.end_time) - MIN(s.start_time)) * 1000",
            "end_time": "MAX(s.end_time)",
        }
        sort_expr = trace_sort_map.get(sort_by, "MIN(s.start_time)")
        sort_dir = "DESC" if sort_order.lower() == "desc" else "ASC"

        query = f"""
            SELECT
                s.trace_id,
                MIN(s.start_time) as start_time,
                MAX(s.end_time) as end_time,
                (MAX(s.end_time) - MIN(s.start_time)) * 1000 as duration_ms,
                CASE WHEN SUM(CASE WHEN s.status = 'ERROR' THEN 1 ELSE 0 END) > 0
                     THEN 'ERROR' ELSE 'OK' END as status,
                s.session_id,
                s.user_id,
                s.app_name,
                s.business_scene,
                COUNT(*) as span_count,
                SUM(CASE WHEN s.span_kind = 'LLM' THEN 1 ELSE 0 END) as llm_call_count,
                SUM(COALESCE(s.input_tokens, 0)) as input_tokens,
                SUM(COALESCE(s.output_tokens, 0)) as output_tokens,
                SUM(COALESCE(s.total_tokens, 0)) as total_tokens,
                s.model,
                s.error_type
            FROM spans s
            WHERE s.trace_id IN ({placeholders})
            GROUP BY s.trace_id
            ORDER BY {sort_expr} {sort_dir}
            LIMIT ? OFFSET ?
        """
        agg_params = list(matching_trace_ids) + [limit, offset]

        rows = conn.execute(query, agg_params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            tid = d["trace_id"]
            # Get input/output summary from first LLM span for this trace
            summary_row = conn.execute(
                """SELECT payload, request_metadata FROM spans
                   WHERE trace_id = ? AND span_kind = 'LLM'
                   ORDER BY start_time LIMIT 1""",
                (tid,)
            ).fetchone()
            if summary_row:
                req_meta = json.loads(summary_row["request_metadata"]) if summary_row["request_metadata"] else {}
                payload = json.loads(summary_row["payload"]) if summary_row["payload"] else {}
                req_payload = payload.get("request", {}) if isinstance(payload, dict) else {}
                resp_payload = payload.get("response", {}) if isinstance(payload, dict) else {}

                messages = req_payload.get("messages", []) if isinstance(req_payload, dict) else []
                if messages and isinstance(messages, list):
                    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
                    if last_user:
                        content = last_user.get("content", "")
                        if isinstance(content, str):
                            d["input_summary"] = content[:200]
                        elif isinstance(content, list):
                            d["input_summary"] = str(content[0].get("text", ""))[:200] if content else ""

                choices = resp_payload.get("choices", []) if isinstance(resp_payload, dict) else []
                if choices and isinstance(choices, list):
                    first_choice = choices[0]
                    msg = first_choice.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        d["output_summary"] = content[:200]

            results.append(d)

        return {"traces": results, "total": total}

    def get_trace_detail(self, trace_id: str) -> Optional[dict]:
        """Get full trace detail with all spans."""
        spans = self.get_trace_spans(trace_id)
        if not spans:
            return None

        # Compute trace-level aggregates
        start_times = [s["start_time"] for s in spans]
        end_times = [s["end_time"] for s in spans]
        trace_start = min(start_times)
        trace_end = max(end_times)
        duration_ms = (trace_end - trace_start) * 1000
        status = "ERROR" if any(s["status"] == "ERROR" for s in spans) else "OK"

        # Root span = span with no parent_span_id
        root_spans = [s for s in spans if not s["parent_span_id"]]
        root_span_id = root_spans[0]["span_id"] if root_spans else spans[0]["span_id"]

        llm_spans = [s for s in spans if s["span_kind"] == "LLM"]
        input_tokens = sum(s.get("input_tokens") or 0 for s in llm_spans)
        output_tokens = sum(s.get("output_tokens") or 0 for s in llm_spans)
        total_tokens = sum(s.get("total_tokens") or 0 for s in llm_spans)

        return {
            "trace_id": trace_id,
            "root_span_id": root_span_id,
            "start_time": trace_start,
            "end_time": trace_end,
            "duration_ms": duration_ms,
            "status": status,
            "session_id": spans[0].get("session_id"),
            "user_id": spans[0].get("user_id"),
            "app_name": spans[0].get("app_name"),
            "business_scene": spans[0].get("business_scene"),
            "span_count": len(spans),
            "llm_call_count": len(llm_spans),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "spans": spans,
        }

    def get_metrics(
        self,
        time_start: Optional[float] = None,
        time_end: Optional[float] = None,
        model: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[float] = None,
    ) -> dict:
        """Compute aggregated metrics for dashboard.

        P0-05: Separates metrics into three levels:
        - Trace metrics: trace_count, error_count, error_rate
        - LLM Call metrics: llm_call_count, latency percentiles, ttft/ttfc, tokens
        - Span metrics: span_count (debugging only)

        P0-04: TTFT = Time To First Token; TTFC = Time To Complete.
        Latency percentiles computed from LLM spans only.
        """
        conn = self._get_conn()

        # Build WHERE clause
        conditions = []
        params: list = []
        if time_start is not None:
            conditions.append("start_time >= ?")
            params.append(time_start)
        if time_end is not None:
            conditions.append("start_time <= ?")
            params.append(time_end)
        if model is not None:
            conditions.append("model = ?")
            params.append(model)
        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # ── Trace-level metrics ──
        trace_row = conn.execute(
            f"""SELECT
                COUNT(DISTINCT trace_id) as trace_count,
                COUNT(DISTINCT CASE WHEN status = 'ERROR' THEN trace_id END) as error_trace_count
            FROM spans WHERE {where_clause}""",
            params
        ).fetchone()

        trace_count = trace_row["trace_count"] or 0
        error_trace_count = trace_row["error_trace_count"] or 0
        error_rate = (error_trace_count / trace_count * 100) if trace_count > 0 else 0.0

        # ── LLM Call-level metrics ──
        llm_where = f"{where_clause} AND span_kind = 'LLM'"
        llm_row = conn.execute(
            f"""SELECT
                COUNT(*) as llm_call_count,
                SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) as llm_error_count,
                SUM(COALESCE(input_tokens, 0)) as input_tokens,
                SUM(COALESCE(output_tokens, 0)) as output_tokens,
                SUM(COALESCE(total_tokens, 0)) as total_tokens,
                COUNT(DISTINCT model) as unique_models,
                COUNT(DISTINCT user_id) as unique_users,
                COUNT(DISTINCT session_id) as unique_sessions
            FROM spans WHERE {llm_where}""",
            params
        ).fetchone()

        llm_call_count = llm_row["llm_call_count"] or 0
        llm_error_count = llm_row["llm_error_count"] or 0
        if trace_count > 0:
            # Keep error_rate at trace level — it's more meaningful
            pass

        # Latency percentiles (LLM spans only)
        latency_rows = conn.execute(
            f"""SELECT duration_ms FROM spans
                WHERE {llm_where} AND duration_ms IS NOT NULL
                ORDER BY duration_ms""",
            params
        ).fetchall()
        latencies = [r["duration_ms"] for r in latency_rows]

        p50 = self._percentile(latencies, 50)
        p95 = self._percentile(latencies, 95)
        p99 = self._percentile(latencies, 99)

        # TTFT percentiles (LLM spans only)
        ttft_rows = conn.execute(
            f"""SELECT ttft_ms FROM spans
                WHERE {llm_where} AND ttft_ms IS NOT NULL
                ORDER BY ttft_ms""",
            params
        ).fetchall()
        ttfts = [r["ttft_ms"] for r in ttft_rows]

        avg_ttft = sum(ttfts) / len(ttfts) if ttfts else None
        p50_ttft = self._percentile(ttfts, 50) if ttfts else None
        p95_ttft = self._percentile(ttfts, 95) if ttfts else None

        # TTFC percentiles (LLM spans only) — P0-04
        ttfc_rows = conn.execute(
            f"""SELECT ttfc_ms FROM spans
                WHERE {llm_where} AND ttfc_ms IS NOT NULL
                ORDER BY ttfc_ms""",
            params
        ).fetchall()
        ttfcs = [r["ttfc_ms"] for r in ttfc_rows]

        avg_ttfc = sum(ttfcs) / len(ttfcs) if ttfcs else None
        p50_ttfc = self._percentile(ttfcs, 50) if ttfcs else None
        p95_ttfc = self._percentile(ttfcs, 95) if ttfcs else None

        # Fallback: if ttfc_ms is not populated, use latency as TTFC
        if not ttfcs and latencies:
            avg_ttfc = sum(latencies) / len(latencies)
            p50_ttfc = p50
            p95_ttfc = p95

        # ── Span-level metrics (debugging) ──
        span_row = conn.execute(
            f"""SELECT COUNT(*) as span_count FROM spans WHERE {where_clause}""",
            params
        ).fetchone()
        span_count = span_row["span_count"] or 0

        return {
            # Trace-level
            "trace_count": trace_count,
            "error_count": error_trace_count,
            "error_rate": round(error_rate, 2),

            # LLM Call-level
            "llm_call_count": llm_call_count,
            "p50_latency_ms": round(p50, 2) if p50 else 0.0,
            "p95_latency_ms": round(p95, 2) if p95 else 0.0,
            "p99_latency_ms": round(p99, 2) if p99 else 0.0,
            "avg_ttft_ms": round(avg_ttft, 2) if avg_ttft else None,
            "p50_ttft_ms": round(p50_ttft, 2) if p50_ttft else None,
            "p95_ttft_ms": round(p95_ttft, 2) if p95_ttft else None,
            "avg_ttfc_ms": round(avg_ttfc, 2) if avg_ttfc else None,
            "p50_ttfc_ms": round(p50_ttfc, 2) if p50_ttfc else None,
            "p95_ttfc_ms": round(p95_ttfc, 2) if p95_ttfc else None,

            # Tokens (LLM spans only)
            "total_input_tokens": llm_row["input_tokens"] or 0,
            "total_output_tokens": llm_row["output_tokens"] or 0,
            "total_tokens": llm_row["total_tokens"] or 0,

            # Span-level (debugging)
            "span_count": span_count,

            # Dimensional
            "unique_models": llm_row["unique_models"] or 0,
            "unique_users": llm_row["unique_users"] or 0,
            "unique_sessions": llm_row["unique_sessions"] or 0,
        }

    def get_time_series(
        self,
        time_start: float,
        time_end: float,
        interval_seconds: int = 60,
        model: Optional[str] = None,
    ) -> list[dict]:
        """Get time series data for charts."""
        conn = self._get_conn()

        conditions = ["start_time >= ?", "start_time <= ?"]
        params: list = [time_start, time_end]
        if model is not None:
            conditions.append("model = ?")
            params.append(model)

        where_clause = " AND ".join(conditions)

        rows = conn.execute(
            f"""SELECT
                CAST(start_time / {interval_seconds} AS INTEGER) * {interval_seconds} as bucket,
                COUNT(DISTINCT trace_id) as trace_count,
                COUNT(CASE WHEN span_kind = 'LLM' THEN 1 END) as llm_call_count,
                COUNT(*) as span_count,
                SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) as errors,
                AVG(duration_ms) as avg_latency,
                SUM(COALESCE(total_tokens, 0)) as tokens,
                AVG(CASE WHEN ttft_ms IS NOT NULL THEN ttft_ms END) as avg_ttft
            FROM spans
            WHERE {where_clause}
            GROUP BY bucket
            ORDER BY bucket""",
            params
        ).fetchall()

        return [dict(r) for r in rows]

    def get_models_list(self) -> list[dict]:
        """Get list of models with counts."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT model,
                      COUNT(*) as span_count,
                      SUM(CASE WHEN span_kind = 'LLM' THEN 1 ELSE 0 END) as llm_call_count,
                      COUNT(DISTINCT trace_id) as trace_count,
                      SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) as errors
               FROM spans WHERE model IS NOT NULL
               GROUP BY model ORDER BY llm_call_count DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_span_count(self) -> int:
        """Get total span count."""
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) as cnt FROM spans").fetchone()
        return row["cnt"]

    @staticmethod
    def _percentile(sorted_values: list, percentile: int) -> Optional[float]:
        """Calculate percentile from a sorted list."""
        if not sorted_values:
            return None
        k = (len(sorted_values) - 1) * percentile / 100
        f = int(k)
        c = k - f
        if f + 1 < len(sorted_values):
            return sorted_values[f] + c * (sorted_values[f + 1] - sorted_values[f])
        return sorted_values[f]
