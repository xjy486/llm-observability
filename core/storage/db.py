"""
SQLite storage layer for the Observability Core.

Stores spans in a single table; traces are derived by aggregating spans
with the same trace_id. Supports incremental assembly for late-arriving spans.

P0-NEW-02: ttfc_ms deleted; first_chunk_ms added alongside ttft_ms.
P0-NEW-03: Trace filter uses CTE — status/duration at trace level, model at span level (EXISTS).
P0-NEW-04: Schema migration via ALTER TABLE + metadata table; ingest returns proper status.
P1-NEW-01: TimeSeries fields match Summary semantics (trace/LLM/span separation).
P1-NEW-02: Pagination via CTE/subquery — no loading all trace_ids into Python.
"""
import sqlite3
import json
import os
import threading
import logging
from typing import Optional
from collections import defaultdict

logger = logging.getLogger("core.storage")

# Current schema version
SCHEMA_VERSION = "2"

# Columns that should exist in the spans table (schema v2)
EXPECTED_SPAN_COLUMNS = {
    "trace_id", "span_id", "parent_span_id", "span_name", "span_kind",
    "start_time", "end_time", "duration_ms", "status", "http_status",
    "ttft_ms", "first_chunk_ms",  # v2: ttfc_ms removed, first_chunk_ms added
    "session_id", "user_id", "app_name", "business_scene",
    "attributes", "events", "error_type", "error_message",
    "payload", "request_metadata", "payload_ref", "trace_inherited",
    "model", "input_tokens", "output_tokens", "total_tokens", "is_stream",
    "created_at",
}

# Columns that may need ALTER TABLE for old (v1) databases.
# The migration checks each against existing columns and only adds missing ones,
# so listing columns that already exist in v1 is harmless.
V2_NEW_COLUMNS = {
    "parent_span_id": "TEXT",
    "span_name": "TEXT",
    "http_status": "INTEGER",
    "first_chunk_ms": "REAL",
    "app_name": "TEXT",
    "business_scene": "TEXT",
    "payload_ref": "TEXT",
    "trace_inherited": "INTEGER DEFAULT 0",
    "model": "TEXT",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "is_stream": "INTEGER",
    "created_at": "REAL",  # ALTER TABLE doesn't allow non-constant defaults
}


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
        """Initialize database schema with migration support.

        P0-NEW-04: Checks existing schema and runs ALTER TABLE for missing columns.
        Old databases with ttfc_ms are migrated — but old timing values are NOT
        copied to new fields (incompatible semantics). first_chunk_ms and ttft_ms
        are set to NULL for legacy records; duration_ms is preserved.
        """
        with self._init_lock:
            if self._initialized:
                return
            conn = self._get_conn()

            # Create metadata table for schema versioning
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

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
                    first_chunk_ms REAL,
                    session_id TEXT,
                    user_id TEXT,
                    app_name TEXT,
                    business_scene TEXT,
                    attributes TEXT,
                    events TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    payload TEXT,
                    request_metadata TEXT,
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
            """)

            # ── P0-NEW-04: Schema Migration ──
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(spans)").fetchall()
            }

            # Add any v2 columns that are missing from the existing table
            for col_name, col_type in V2_NEW_COLUMNS.items():
                if col_name not in existing_cols:
                    logger.info("Schema migration: adding column %s %s", col_name, col_type)
                    conn.execute(f"ALTER TABLE spans ADD COLUMN {col_name} {col_type}")

                    # P0-NEW-02-fix: Do NOT copy old ttfc_ms → first_chunk_ms.
                    # Old timing semantics (ttfc_ms) are incompatible with new
                    # semantics (first_chunk_ms / ttft_ms). Setting them to NULL
                    # is the safest choice — old records keep duration_ms but lose
                    # streaming-specific timing fields rather than carrying wrong data.
                    if col_name == "first_chunk_ms" and "ttfc_ms" in existing_cols:
                        logger.info(
                            "Migration: old ttfc_ms column detected — leaving "
                            "first_chunk_ms NULL (incompatible timing semantics)"
                        )

            # ── Destructive migration: v1 → v2 timing semantics ──
            # CRITICAL: This must only run ONCE. The old ttfc_ms column is never
            # DROPped (SQLite ALTER TABLE DROP COLUMN requires v3.35+), so
            # "ttfc_ms" in existing_cols stays True forever. If we NULL ttft_ms
            # on every restart, we destroy new valid timing data produced by
            # the v2 system. Gate on schema_version instead: run only when
            # upgrading from a prior version (< SCHEMA_VERSION).
            version_row = conn.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            current_version = version_row[0] if version_row else None

            if current_version != SCHEMA_VERSION and "ttfc_ms" in existing_cols:
                conn.execute(
                    "UPDATE spans SET ttft_ms = NULL WHERE ttft_ms IS NOT NULL"
                )
                logger.info(
                    "Migration v1→v2: NULLed legacy ttft_ms values "
                    "(incompatible timing semantics, one-time only)"
                )

            # Create indexes AFTER migration so all columns exist
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
                CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time);
                CREATE INDEX IF NOT EXISTS idx_spans_status ON spans(status);
                CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id);
                CREATE INDEX IF NOT EXISTS idx_spans_user ON spans(user_id);
                CREATE INDEX IF NOT EXISTS idx_spans_model ON spans(model);
                CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(span_kind);
            """)

            # Record schema version
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )
            conn.commit()
            self._initialized = True
            logger.info("Storage initialized (schema v%s): %s", SCHEMA_VERSION, self.db_path)

    def insert_span(self, record: dict) -> bool:
        """Insert or replace a span record.

        P0-NEW-02: Uses first_chunk_ms instead of ttfc_ms.
        """
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
                ttft_ms, first_chunk_ms, session_id, user_id, app_name, business_scene,
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
            record.get("first_chunk_ms"),
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

        P0-NEW-03 (fix): TRUE EXISTS semantics:
        - status: trace-level aggregate — computed from ALL spans of matching traces
        - min/max_duration_ms: trace-level aggregate — computed from ALL spans
        - model/session_id/user_id/app_name/business_scene: span-level EXISTS subquery
          (trace qualifies if ≥1 span matches; but aggregation uses ALL spans)

        P1-NEW-02: Pagination via CTE — no loading all trace_ids into Python.

        Returns:
            {"traces": [...], "total": int}
        """
        conn = self._get_conn()

        # ── Build trace-qualification WHERE clause ──
        # Direct conditions (time, trace_id) filter which traces are "in scope"
        direct_conditions = []
        direct_params: list = []

        if time_start is not None:
            direct_conditions.append("s.start_time >= ?")
            direct_params.append(time_start)
        if time_end is not None:
            direct_conditions.append("s.start_time <= ?")
            direct_params.append(time_end)
        if trace_id is not None:
            direct_conditions.append("s.trace_id = ?")
            direct_params.append(trace_id)

        # EXISTS conditions: trace has ≥1 span matching the attribute
        # These do NOT restrict which spans get aggregated — they only determine
        # which trace_ids qualify.
        exists_clauses = []
        exists_params: list = []

        if model is not None:
            exists_clauses.append(
                "EXISTS (SELECT 1 FROM spans s2 WHERE s2.trace_id = s.trace_id AND s2.model = ?)"
            )
            exists_params.append(model)
        if session_id is not None:
            exists_clauses.append(
                "EXISTS (SELECT 1 FROM spans s2 WHERE s2.trace_id = s.trace_id AND s2.session_id = ?)"
            )
            exists_params.append(session_id)
        if user_id is not None:
            exists_clauses.append(
                "EXISTS (SELECT 1 FROM spans s2 WHERE s2.trace_id = s.trace_id AND s2.user_id = ?)"
            )
            exists_params.append(user_id)
        if app_name is not None:
            exists_clauses.append(
                "EXISTS (SELECT 1 FROM spans s2 WHERE s2.trace_id = s.trace_id AND s2.app_name = ?)"
            )
            exists_params.append(app_name)
        if business_scene is not None:
            exists_clauses.append(
                "EXISTS (SELECT 1 FROM spans s2 WHERE s2.trace_id = s.trace_id AND s2.business_scene = ?)"
            )
            exists_params.append(business_scene)

        all_conditions = direct_conditions + exists_clauses
        all_params = direct_params + exists_params
        trace_where = " AND ".join(all_conditions) if all_conditions else "1=1"

        # ── HAVING: trace-level aggregate filters (status, duration) ──
        having_conditions = []
        having_params: list = []

        if status is not None:
            having_conditions.append("trace_status = ?")
            having_params.append(status)
        if min_duration_ms is not None:
            having_conditions.append("trace_duration_ms >= ?")
            having_params.append(min_duration_ms)
        if max_duration_ms is not None:
            having_conditions.append("trace_duration_ms <= ?")
            having_params.append(max_duration_ms)

        having_clause = " AND ".join(having_conditions) if having_conditions else "1=1"

        # Valid sort columns (map to trace-level aggregate expressions)
        trace_sort_map = {
            "start_time": "trace_start",
            "duration_ms": "trace_duration_ms",
            "end_time": "trace_end",
        }
        sort_expr = trace_sort_map.get(sort_by, "trace_start")
        sort_dir = "DESC" if sort_order.lower() == "desc" else "ASC"

        # The CTE template: qualified_traces finds trace_ids; trace_agg computes
        # MIN/MAX/status from ALL spans of those traces; HAVING filters at trace level.
        cte_template = f"""
            WITH qualified_traces AS (
                SELECT DISTINCT s.trace_id
                FROM spans s
                WHERE {trace_where}
            ),
            trace_agg AS (
                SELECT
                    trace_id,
                    MIN(start_time) AS trace_start,
                    MAX(end_time) AS trace_end,
                    (MAX(end_time) - MIN(start_time)) * 1000 AS trace_duration_ms,
                    CASE
                        WHEN SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) > 0
                        THEN 'ERROR' ELSE 'OK'
                    END AS trace_status
                FROM spans
                WHERE trace_id IN (SELECT trace_id FROM qualified_traces)
                GROUP BY trace_id
                HAVING {having_clause}
            )
        """

        # ── Count query ──
        count_query = f"{cte_template} SELECT COUNT(*) AS total FROM trace_agg"
        count_params = all_params + having_params
        total_row = conn.execute(count_query, count_params).fetchone()
        total = total_row["total"] if total_row else 0

        if total == 0:
            return {"traces": [], "total": 0}

        # ── Page query (get ordered trace_ids) ──
        page_query = f"{cte_template} SELECT trace_id FROM trace_agg ORDER BY {sort_expr} {sort_dir} LIMIT ? OFFSET ?"
        page_params = all_params + having_params + [limit, offset]
        page_rows = conn.execute(page_query, page_params).fetchall()
        page_trace_ids = [r["trace_id"] for r in page_rows]

        if not page_trace_ids:
            return {"traces": [], "total": total}

        # ── Phase 2: Aggregate ALL spans for the page's trace_ids ──
        placeholders = ",".join("?" * len(page_trace_ids))

        query = f"""
            SELECT
                s.trace_id,
                MIN(s.start_time) as start_time,
                MAX(s.end_time) as end_time,
                (MAX(s.end_time) - MIN(s.start_time)) * 1000 as duration_ms,
                CASE WHEN SUM(CASE WHEN s.status = 'ERROR' THEN 1 ELSE 0 END) > 0
                     THEN 'ERROR' ELSE 'OK' END as status,
                MAX(CASE WHEN s.span_kind = 'AGENT' THEN s.session_id END) as session_id,
                MAX(CASE WHEN s.span_kind = 'AGENT' THEN s.user_id END) as user_id,
                MAX(CASE WHEN s.span_kind = 'AGENT' THEN s.app_name END) as app_name,
                MAX(CASE WHEN s.span_kind = 'AGENT' THEN s.business_scene END) as business_scene,
                COUNT(*) as span_count,
                SUM(CASE WHEN s.span_kind = 'LLM' THEN 1 ELSE 0 END) as llm_call_count,
                SUM(CASE WHEN s.span_kind = 'LLM' THEN COALESCE(s.input_tokens, 0) ELSE 0 END) as input_tokens,
                SUM(CASE WHEN s.span_kind = 'LLM' THEN COALESCE(s.output_tokens, 0) ELSE 0 END) as output_tokens,
                SUM(CASE WHEN s.span_kind = 'LLM' THEN COALESCE(s.total_tokens, 0) ELSE 0 END) as total_tokens,
                MAX(CASE WHEN s.model IS NOT NULL THEN s.model END) as model,
                MAX(CASE WHEN s.error_type IS NOT NULL THEN s.error_type END) as error_type
            FROM spans s
            WHERE s.trace_id IN ({placeholders})
            GROUP BY s.trace_id
        """
        agg_params = list(page_trace_ids)

        rows = conn.execute(query, agg_params).fetchall()

        # Sort results in Python to match the CTE ordering (since IN() doesn't preserve order)
        # Build a sort key mapping
        sort_index = {tid: i for i, tid in enumerate(page_trace_ids)}
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

            results.append((sort_index.get(tid, 0), d))

        # Sort by the CTE ordering
        results.sort(key=lambda x: x[0])
        final_results = [d for _, d in results]

        return {"traces": final_results, "total": total}

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

        # Trace-level metadata: prefer root/AGENT span, fallback to first non-NULL
        meta_span = root_spans[0] if root_spans else spans[0]
        agent_spans = [s for s in spans if s["span_kind"] == "AGENT"]
        if agent_spans:
            meta_span = agent_spans[0]

        def _first_non_null(key):
            for s in spans:
                if s.get(key):
                    return s[key]
            return None

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
            "session_id": meta_span.get("session_id") or _first_non_null("session_id"),
            "user_id": meta_span.get("user_id") or _first_non_null("user_id"),
            "app_name": meta_span.get("app_name") or _first_non_null("app_name"),
            "business_scene": meta_span.get("business_scene") or _first_non_null("business_scene"),
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
        """Compute aggregated metrics for dashboard.

        P0-NEW-02: TTFC metrics removed. Only duration_ms, ttft_ms, first_chunk_ms.
        Metrics separated into three levels: Trace / LLM Call / Span.
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
        # Trace status: any span ERROR → trace ERROR
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

        # TTFT percentiles (LLM spans only, streaming only — ttft_ms is null for non-streaming)
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

        # P0-NEW-02: TTFC metrics removed — duration_ms already represents total latency

        # P0-NEW-02: first_chunk_ms percentiles (LLM spans only, streaming only)
        first_chunk_rows = conn.execute(
            f"""SELECT first_chunk_ms FROM spans
                WHERE {llm_where} AND first_chunk_ms IS NOT NULL
                ORDER BY first_chunk_ms""",
            params
        ).fetchall()
        first_chunks = [r["first_chunk_ms"] for r in first_chunk_rows]

        avg_first_chunk = sum(first_chunks) / len(first_chunks) if first_chunks else None
        p50_first_chunk = self._percentile(first_chunks, 50) if first_chunks else None
        p95_first_chunk = self._percentile(first_chunks, 95) if first_chunks else None

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
            "avg_first_chunk_ms": round(avg_first_chunk, 2) if avg_first_chunk else None,
            "p50_first_chunk_ms": round(p50_first_chunk, 2) if p50_first_chunk else None,
            "p95_first_chunk_ms": round(p95_first_chunk, 2) if p95_first_chunk else None,

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
        """Get time series data for charts.

        P1-NEW-01: TimeSeries fields match Summary semantics:
        - trace_count: distinct traces in bucket
        - trace_error_count: traces with any ERROR span
        - llm_call_count: LLM spans in bucket
        - llm_error_count: ERROR LLM spans
        - llm_avg_latency_ms: avg duration_ms of LLM spans
        - avg_ttft_ms: avg ttft_ms of LLM spans (streaming only)
        - span_count: total spans in bucket
        - tokens: sum of total_tokens
        """
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
                SUM(CASE WHEN status = 'ERROR' AND span_kind = 'LLM' THEN 1 ELSE 0 END) as llm_error_count,
                AVG(CASE WHEN span_kind = 'LLM' THEN duration_ms END) as llm_avg_latency_ms,
                SUM(CASE WHEN span_kind = 'LLM' THEN COALESCE(total_tokens, 0) ELSE 0 END) as tokens,
                AVG(CASE WHEN span_kind = 'LLM' AND ttft_ms IS NOT NULL THEN ttft_ms END) as avg_ttft_ms,
                AVG(CASE WHEN span_kind = 'LLM' AND first_chunk_ms IS NOT NULL THEN first_chunk_ms END) as avg_first_chunk_ms
            FROM spans
            WHERE {where_clause}
            GROUP BY bucket
            ORDER BY bucket""",
            params
        ).fetchall()

        # Compute trace_error_count separately per bucket (traces with any ERROR span)
        result = []
        for r in rows:
            d = dict(r)
            bucket = d["bucket"]
            # Count distinct traces with ERROR status in this bucket
            err_row = conn.execute(
                f"""SELECT COUNT(DISTINCT trace_id) as cnt
                    FROM spans
                    WHERE {where_clause}
                      AND status = 'ERROR'
                      AND CAST(start_time / {interval_seconds} AS INTEGER) * {interval_seconds} = ?""",
                params + [bucket]
            ).fetchone()
            d["trace_error_count"] = err_row["cnt"] if err_row else 0
            result.append(d)

        return result

    def get_models_list(self) -> list[dict]:
        """Get list of models with counts.

        P0-NEW-01: ModelInfo fields aligned with frontend:
        - model, trace_count, llm_call_count, span_count, llm_errors
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT model,
                      COUNT(*) as span_count,
                      SUM(CASE WHEN span_kind = 'LLM' THEN 1 ELSE 0 END) as llm_call_count,
                      COUNT(DISTINCT trace_id) as trace_count,
                      SUM(CASE WHEN status = 'ERROR' AND span_kind = 'LLM' THEN 1 ELSE 0 END) as llm_errors
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
