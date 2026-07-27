"""
Data models for the Observability Core.

Based on the PRD's Trace/Span semantics and OpenTelemetry GenAI conventions.

P0-NEW-02: Timing semantics —
  ttft_ms        = Time To First Token (streaming only; NULL for non-streaming)
  first_chunk_ms = Time To First SSE Chunk (streaming only; NULL for non-streaming)
  duration_ms    = Total request duration (always set)
  ttfc_ms is REMOVED — it was redundant with duration_ms.
"""
from datetime import datetime, timezone
from typing import Optional, Any
from pydantic import BaseModel, Field


class Event(BaseModel):
    """A point-in-time event within a span."""
    name: str
    timestamp: float  # Unix epoch
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanRecord(BaseModel):
    """A single span record stored in the system."""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    span_name: str
    span_kind: str  # AGENT / LLM / TOOL / GATEWAY / INTERNAL
    start_time: float  # Unix epoch
    end_time: float
    duration_ms: float
    status: str  # OK / ERROR / UNSET
    http_status: Optional[int] = None
    ttft_ms: Optional[float] = None  # Time To First Token (streaming only, NULL for non-streaming)
    first_chunk_ms: Optional[float] = None  # Time To First SSE Chunk (streaming only, NULL for non-streaming)
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    app_name: Optional[str] = None
    business_scene: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[Event] = Field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    request_metadata: Optional[dict[str, Any]] = None
    payload_ref: Optional[str] = None  # reference to external payload store
    trace_inherited: bool = False

    # Computed/derived
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    is_stream: Optional[bool] = None


class IngestRecord(BaseModel):
    """Incoming telemetry record from proxy/SDK."""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    trace_inherited: bool = False
    span_name: str = "llm.completion"
    span_kind: str = "LLM"
    start_time: float
    end_time: float
    duration_ms: float
    status: str = "OK"
    http_status: Optional[int] = None
    ttft_ms: Optional[float] = None  # Time To First Token (streaming only)
    first_chunk_ms: Optional[float] = None  # Time To First SSE Chunk (streaming only)
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    app_name: Optional[str] = None
    business_scene: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    request_metadata: Optional[dict[str, Any]] = None

    # Accept ttfc_ms for backward compatibility (ignored)
    ttfc_ms: Optional[float] = None


class IngestRequest(BaseModel):
    """Batch ingest request."""
    records: list[IngestRecord]


class TraceSummary(BaseModel):
    """Trace summary for list view."""
    trace_id: str
    root_span_id: str
    start_time: float
    end_time: float
    duration_ms: float
    status: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    app_name: Optional[str] = None
    business_scene: Optional[str] = None
    span_count: int = 0
    llm_call_count: int = 0
    tool_call_count: int = 0  # P1-5: Phase 2.2 tool span count
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: Optional[str] = None
    input_summary: Optional[str] = None
    output_summary: Optional[str] = None
    error_type: Optional[str] = None


class TraceDetail(BaseModel):
    """Full trace detail with span tree."""
    trace_id: str
    root_span_id: str
    start_time: float
    end_time: float
    duration_ms: float
    status: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    app_name: Optional[str] = None
    business_scene: Optional[str] = None
    span_count: int = 0
    llm_call_count: int = 0
    tool_call_count: int = 0  # P1-5: Phase 2.2 tool span count
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    spans: list[SpanRecord] = Field(default_factory=list)


class MetricsSummary(BaseModel):
    """Dashboard metrics summary.

    Metrics are separated into three levels:
    - Trace metrics: trace_count, error_rate (at trace level)
    - LLM Call metrics: llm_call_count, latency percentiles, ttft/first_chunk, tokens (LLM spans only)
    - Tool metrics: tool_call_count, tool_error_rate, tool latency percentiles (Phase 2.2)
    - Span metrics: span_count (debugging only)

    P0-NEW-02: ttfc_ms replaced by first_chunk_ms.
    P1-5: Tool metrics fields added for Phase 2.2 contract sync.
    """
    # Trace-level metrics
    trace_count: int = 0
    error_count: int = 0
    error_rate: float = 0.0

    # LLM Call-level metrics (only span_kind='LLM')
    llm_call_count: int = 0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    avg_ttft_ms: Optional[float] = None
    p50_ttft_ms: Optional[float] = None
    p95_ttft_ms: Optional[float] = None
    avg_first_chunk_ms: Optional[float] = None
    p50_first_chunk_ms: Optional[float] = None
    p95_first_chunk_ms: Optional[float] = None

    # Token metrics (LLM spans only)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0

    # Tool-level metrics (Phase 2.2, P1-5)
    tool_call_count: int = 0
    tool_error_count: int = 0
    tool_error_rate: float = 0.0
    p50_tool_latency_ms: float = 0.0
    p95_tool_latency_ms: float = 0.0
    p99_tool_latency_ms: float = 0.0

    # Span-level metrics (debugging)
    span_count: int = 0

    # Dimensional metrics
    unique_models: int = 0
    unique_users: int = 0
    unique_sessions: int = 0