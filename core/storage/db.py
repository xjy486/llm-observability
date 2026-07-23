"""
SQLite storage layer for the Observability Core.

Stores spans in a single table; traces are derived by aggregating spans
with the same trace_id. Supports incremental assembly for late-arriving spans.
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
                ttft_ms, session_id, user_id, app_name, business_scene,
                attributes, events, error_type, error_message, payload,
                request_metadata, payload_ref, trace_inherited,
                model, input_tokens, output_tokens, total_tokens, is_stream
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ) -> list[dict]:
        """Get trace summaries with filtering."""
        conn = self._get_conn()

        # Build query for trace-level aggregation
        conditions = []
        params = []

        if time_start is not None:
            conditions.append("s.start_time >= ?")
            params.append(time_start)
        if time_end is not None:
            conditions.append("s.start_time <= ?")
            params.append(time_end)
        if status is not None:
            conditions.append("s.status = ?")
            params.append(status)
        if model is not None:
            conditions.append("s.model = ?")
            params.append(model)
        if session_id is not None:
            conditions.append("s.session_id = ?")
            params.append(session_id)
        if user_id is not None:
            conditions.append("s.user_id = ?")
            params.append(user_id)
        if trace_id is not None:
            conditions.append("s.trace_id = ?")
            params.append(trace_id)
        if min_duration_ms is not None:
            conditions.append("s.duration_ms >= ?")
            params.append(min_duration_ms)
        if max_duration_ms is not None:
            conditions.append("s.duration_ms <= ?")
            params.append(max_duration_ms)
        if app_name is not None:
            conditions.append("s.app_name = ?")
            params.append(app_name)
        if business_scene is not None:
            conditions.append("s.business_scene = ?")
            params.append(business_scene)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Valid sort columns
        valid_sort = {"start_time", "duration_ms", "end_time"}
        sort_col = sort_by if sort_by in valid_sort else "start_time"
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
            WHERE {where_clause}
            GROUP BY s.trace_id
            ORDER BY {sort_col} {sort_dir}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            # Get input/output summary from first LLM span
            trace_id = d["trace_id"]
            summary_row = conn.execute(
                """SELECT payload, request_metadata FROM spans
                   WHERE trace_id = ? AND span_kind = 'LLM'
                   ORDER BY start_time LIMIT 1""",
                (trace_id,)
            ).fetchone()
            if summary_row:
                req_meta = json.loads(summary_row["request_metadata"]) if summary_row["request_metadata"] else {}
                payload = json.loads(summary_row["payload"]) if summary_row["payload"] else {}
                req_payload = payload.get("request", {}) if isinstance(payload, dict) else {}
                resp_payload = payload.get("response", {}) if isinstance(payload, dict) else {}

                # Build summaries
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

        return results

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
        user_id: Optional[str] = None,
    ) -> dict:
        """Compute aggregated metrics for dashboard."""
        conn = self._get_conn()

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

        # Basic counts
        row = conn.execute(
            f"""SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) as errors,
                SUM(COALESCE(input_tokens, 0)) as input_tokens,
                SUM(COALESCE(output_tokens, 0)) as output_tokens,
                SUM(COALESCE(total_tokens, 0)) as total_tokens,
                COUNT(DISTINCT model) as unique_models,
                COUNT(DISTINCT user_id) as unique_users,
                COUNT(DISTINCT session_id) as unique_sessions
            FROM spans WHERE {where_clause}""",
            params
        ).fetchone()

        total = row["total"] or 0
        errors = row["errors"] or 0
        error_rate = (errors / total * 100) if total > 0 else 0.0

        # Latency percentiles
        latency_row = conn.execute(
            f"""SELECT duration_ms FROM spans
                WHERE {where_clause} AND duration_ms IS NOT NULL
                ORDER BY duration_ms""",
            params
        ).fetchall()
        latencies = [r["duration_ms"] for r in latency_row]

        p50 = self._percentile(latencies, 50)
        p95 = self._percentile(latencies, 95)
        p99 = self._percentile(latencies, 99)

        # TTFT percentiles
        ttft_row = conn.execute(
            f"""SELECT ttft_ms FROM spans
                WHERE {where_clause} AND ttft_ms IS NOT NULL
                ORDER BY ttft_ms""",
            params
        ).fetchall()
        ttfts = [r["ttft_ms"] for r in ttft_row]

        avg_ttft = sum(ttfts) / len(ttfts) if ttfts else None
        p50_ttft = self._percentile(ttfts, 50) if ttfts else None
        p95_ttft = self._percentile(ttfts, 95) if ttfts else None

        return {
            "total_requests": total,
            "error_count": errors,
            "error_rate": round(error_rate, 2),
            "p50_latency_ms": round(p50, 2) if p50 else 0.0,
            "p95_latency_ms": round(p95, 2) if p95 else 0.0,
            "p99_latency_ms": round(p99, 2) if p99 else 0.0,
            "avg_ttft_ms": round(avg_ttft, 2) if avg_ttft else None,
            "p50_ttft_ms": round(p50_ttft, 2) if p50_ttft else None,
            "p95_ttft_ms": round(p95_ttft, 2) if p95_ttft else None,
            "total_input_tokens": row["input_tokens"] or 0,
            "total_output_tokens": row["output_tokens"] or 0,
            "total_tokens": row["total_tokens"] or 0,
            "unique_models": row["unique_models"] or 0,
            "unique_users": row["unique_users"] or 0,
            "unique_sessions": row["unique_sessions"] or 0,
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
                COUNT(*) as count,
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
            """SELECT model, COUNT(*) as count,
                      SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) as errors
               FROM spans WHERE model IS NOT NULL
               GROUP BY model ORDER BY count DESC"""
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
