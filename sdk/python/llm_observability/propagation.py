"""W3C Trace Context propagation.

Handles traceparent header injection and extraction, plus ownership
marker headers for span dedup (spec §7.3).
"""
import re
from typing import Optional
from dataclasses import dataclass

from .context import SpanContext
from .utils.ids import generate_span_id


TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
    re.IGNORECASE,
)

# Ownership marker header — spec §7.3
OWNERSHIP_HEADER = "X-LLM-OBS-Span-Role"


def inject_traceparent(ctx: SpanContext) -> str:
    """Serialize a SpanContext into a W3C traceparent header value.

    Args:
        ctx: The active span context.

    Returns:
        W3C traceparent string: 00-{trace_id}-{span_id}-{flags}
    """
    flags = "01" if ctx.sampled else "00"
    return f"00-{ctx.trace_id}-{ctx.span_id}-{flags}"


@dataclass
class ExtractedContext:
    """Result of extracting traceparent — includes inherited flag."""
    trace_id: str
    parent_span_id: str
    span_id: str  # new span_id generated for this hop
    trace_flags: str
    inherited: bool


def extract_traceparent(header_value: str) -> Optional[ExtractedContext]:
    """Parse a W3C traceparent header into context info.

    Args:
        header_value: The raw traceparent header string.

    Returns:
        ExtractedContext if valid, None otherwise.
    """
    if not header_value:
        return None

    header_value = header_value.strip()
    m = TRACEPARENT_RE.match(header_value)
    if not m:
        return None

    version, trace_id, parent_span_id, trace_flags = m.groups()

    if trace_id == "0" * 32:
        return None
    if parent_span_id == "0" * 16:
        return None

    return ExtractedContext(
        trace_id=trace_id.lower(),
        parent_span_id=parent_span_id.lower(),
        span_id=generate_span_id(),
        trace_flags=trace_flags.lower(),
        inherited=True,
    )


def inject_headers(
    ctx: SpanContext,
    is_logical_llm: bool = False,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    app_name: Optional[str] = None,
    business_scene: Optional[str] = None,
    message_id: Optional[str] = None,
) -> dict:
    """Build headers for downstream propagation.

    Includes traceparent, optional ownership marker, and metadata headers.
    Phase 2.5 (P0-5): also emits a `baggage` header carrying association
    metadata (user/session_id/message_id/business_scenario) so the downstream
    Gateway/Server can inherit Association Properties.

    Args:
        ctx: The active span context.
        is_logical_llm: If True, inject ownership marker header.
        session_id: Optional session ID for grouping.
        user_id: Optional user ID.
        app_name: Optional application name.
        business_scene: Optional business scene tag.
        message_id: Optional message_id (Phase 2.5 association).

    Returns:
        Dict of headers to inject into the downstream request.
    """
    headers = {"traceparent": inject_traceparent(ctx)}

    if is_logical_llm:
        headers[OWNERSHIP_HEADER] = "llm"

    if session_id:
        headers["X-Session-Id"] = session_id
    if user_id:
        headers["X-User-Id"] = user_id
    if app_name:
        headers["X-App-Name"] = app_name
    if business_scene:
        headers["X-Business-Scene"] = business_scene

    # P0-5: baggage carries association metadata (incl. message_id) for the
    # downstream Gateway/Server to inherit.
    # Blocker 2.1: use the unified W3C percent-encoding contract so special
    # characters (comma/equals/space/unicode/control) are safely encoded.
    from .association_propagation import encode_baggage_value
    baggage_parts = []
    if user_id:
        baggage_parts.append(f"user={encode_baggage_value(user_id)}")
    if session_id:
        baggage_parts.append(f"session_id={encode_baggage_value(session_id)}")
    if business_scene:
        baggage_parts.append(f"business_scenario={encode_baggage_value(business_scene)}")
    if message_id:
        baggage_parts.append(f"message_id={encode_baggage_value(message_id)}")
    if baggage_parts:
        headers["baggage"] = ",".join(baggage_parts)

    return headers
