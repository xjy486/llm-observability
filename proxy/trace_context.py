"""
W3C Trace Context parsing and propagation.

Implements traceparent header parsing per W3C recommendation:
  version-trace_id-parent_id-trace_flags

If valid upstream traceparent is present, inherit trace_id and parent.
Otherwise, generate a new trace_id and root span_id.
"""
import uuid
import re
from dataclasses import dataclass
from typing import Optional


TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
    re.IGNORECASE,
)


@dataclass
class TraceContext:
    """Represents a parsed or generated trace context."""
    trace_id: str  # 32 hex chars
    span_id: str   # 16 hex chars (current span)
    parent_span_id: Optional[str]  # 16 hex chars (parent, None for root)
    trace_flags: str  # 2 hex chars
    inherited: bool  # True if inherited from upstream traceparent

    def to_traceparent(self) -> str:
        """Serialize back to traceparent header format."""
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    @property
    def sampled(self) -> bool:
        """P0-2: Whether this trace is sampled, derived from trace_flags.

        flags=01 means sampled, flags=00 means not sampled.
        """
        return self.trace_flags == "01"


def parse_traceparent(header_value: str) -> Optional[TraceContext]:
    """Parse a W3C traceparent header value.

    Returns None if invalid/malformed.
    """
    if not header_value:
        return None

    header_value = header_value.strip()
    m = TRACEPARENT_RE.match(header_value)
    if not m:
        return None

    version, trace_id, parent_span_id, trace_flags = m.groups()

    # Validate per W3C spec
    if trace_id == "0" * 32:
        return None  # invalid trace_id
    if parent_span_id == "0" * 16:
        return None  # invalid parent_span_id

    # Generate a new span_id for this proxy span; upstream parent becomes parent_span_id
    new_span_id = uuid.uuid4().hex[:16]

    return TraceContext(
        trace_id=trace_id.lower(),
        span_id=new_span_id,
        parent_span_id=parent_span_id.lower(),
        trace_flags=trace_flags.lower(),
        inherited=True,
    )


def create_trace_context() -> TraceContext:
    """Create a new root trace context (no upstream traceparent)."""
    trace_id = uuid.uuid4().hex  # 32 hex chars
    span_id = uuid.uuid4().hex[:16]  # 16 hex chars
    return TraceContext(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        trace_flags="01",  # sampled
        inherited=False,
    )


def resolve_trace_context(headers: dict) -> TraceContext:
    """Resolve trace context from request headers.

    Priority:
    1. W3C traceparent header (if valid) -> inherit
    2. No valid traceparent -> create new root trace
    """
    # Check for traceparent (case-insensitive)
    traceparent = None
    for k, v in headers.items():
        if k.lower() == "traceparent":
            traceparent = v
            break

    if traceparent:
        ctx = parse_traceparent(traceparent)
        if ctx:
            return ctx

    # No valid upstream context — create fallback trace
    return create_trace_context()


def extract_metadata_headers(headers: dict) -> dict:
    """Extract session_id, user_id, and other metadata from custom headers."""
    meta = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl == "x-session-id":
            meta["session_id"] = v
        elif kl == "x-user-id":
            meta["user_id"] = v
        elif kl == "x-app-name" or kl == "x-service-name":
            meta["app_name"] = v
        elif kl == "x-business-scene":
            meta["business_scene"] = v
        elif kl.startswith("x-trace-"):
            # Custom trace attributes
            attr_key = kl.replace("x-trace-", "")
            meta[f"attr_{attr_key}"] = v
    return meta


def extract_ownership(headers: dict) -> Optional[str]:
    """Extract span ownership marker from headers (spec §7.3).

    Checks for the X-LLM-OBS-Span-Role header. When present and set to 'llm',
    it means the upstream SDK has already created a logical LLM span, so the
    proxy should create a GATEWAY span instead of a duplicate LLM span.

    Args:
        headers: Request headers dict.

    Returns:
        'llm' if the marker is present, None otherwise.
    """
    for k, v in headers.items():
        if k.lower() == "x-llm-obs-span-role":
            return v.strip().lower()
    return None