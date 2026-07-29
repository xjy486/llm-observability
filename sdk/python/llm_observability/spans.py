"""Span type definitions and constants."""
from dataclasses import dataclass, field
from typing import Optional, Any
import time


class SpanKind:
    """Span kind constants."""
    AGENT = "AGENT"
    LLM = "LLM"
    TOOL = "TOOL"
    GATEWAY = "GATEWAY"
    INTERNAL = "INTERNAL"
    TASK = "TASK"  # Phase 2.5: unified @chain/@task/client_call


@dataclass
class Span:
    """A single span with lifecycle management.

    Attributes:
        trace_id: W3C trace ID (32 hex).
        span_id: W3C span ID (16 hex).
        parent_span_id: Parent span ID, None for root.
        span_name: Human-readable span name (e.g. 'agent.run', 'llm.completion').
        span_kind: One of SpanKind constants.
        start_time: Unix epoch seconds (set on start).
        end_time: Unix epoch seconds (set on end).
        status: 'OK', 'ERROR', or 'UNSET'.
        attributes: Arbitrary key-value metadata.
        events: List of event dicts.
        session_id: Optional session ID for grouping.
        user_id: Optional user ID.
        app_name: Application name.
        business_scene: Optional business scene tag.
        error_type: Exception type if status is ERROR.
        error_message: Exception message if status is ERROR.
    """
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    span_name: str
    span_kind: str
    start_time: float = 0.0
    end_time: float = 0.0
    status: str = "UNSET"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    app_name: Optional[str] = None
    business_scene: Optional[str] = None
    message_id: Optional[str] = None  # Phase 2.5: Association Properties
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    request_metadata: Optional[dict[str, Any]] = None

    def start(self):
        """Mark the span as started."""
        self.start_time = time.time()

    def end(self):
        """Mark the span as ended."""
        self.end_time = time.time()

    @property
    def duration_ms(self) -> float:
        """Duration in milliseconds."""
        if self.end_time > 0 and self.start_time > 0:
            return round((self.end_time - self.start_time) * 1000, 2)
        return 0.0

    def set_status(self, status: str):
        """Set span status."""
        self.status = status

    def set_error(self, error_type: str, error_message: str):
        """Set error status and details."""
        self.status = "ERROR"
        self.error_type = error_type
        self.error_message = error_message

    def set_attribute(self, key: str, value: Any):
        """Set a single attribute."""
        self.attributes[key] = value

    def add_event(self, name: str, timestamp: float = None, attributes: dict = None):
        """Add a timestamped event."""
        self.events.append({
            "name": name,
            "timestamp": timestamp or time.time(),
            "attributes": attributes or {},
        })

    def to_record(self) -> dict:
        """Convert to Canonical Telemetry Record for reporting."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "span_name": self.span_name,
            "span_kind": self.span_kind,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "app_name": self.app_name,
            "business_scene": self.business_scene,
            "message_id": self.message_id,
            "attributes": self.attributes,
            "events": self.events,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "payload": self.payload,
            "request_metadata": self.request_metadata,
        }