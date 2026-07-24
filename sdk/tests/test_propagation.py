"""Tests for W3C traceparent propagation."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from llm_observability.propagation import (
    inject_traceparent,
    extract_traceparent,
    inject_headers,
    TRACEPARENT_RE,
)
from llm_observability.context import SpanContext
from llm_observability.utils.ids import generate_trace_id, generate_span_id


def test_inject_traceparent_format():
    """traceparent must be W3C compliant: 00-{32hex}-{16hex}-{2hex}."""
    ctx = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=None,
        span_kind="LLM",
        sampled=True,
    )
    tp = inject_traceparent(ctx)
    assert TRACEPARENT_RE.match(tp), f"Invalid traceparent: {tp}"


def test_inject_traceparent_values():
    """traceparent must contain the context's trace_id and span_id."""
    ctx = SpanContext(
        trace_id="0af7651916cd43dd8448eb211c80319c",
        span_id="b7ad6b7169203331",
        parent_span_id=None,
        span_kind="LLM",
        sampled=True,
    )
    tp = inject_traceparent(ctx)
    assert "0af7651916cd43dd8448eb211c80319c" in tp
    assert "b7ad6b7169203331" in tp


def test_extract_traceparent_valid():
    """Extract a valid traceparent into SpanContext."""
    tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    ctx = extract_traceparent(tp)
    assert ctx is not None
    assert ctx.trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert ctx.parent_span_id == "b7ad6b7169203331"
    assert ctx.inherited is True


def test_extract_traceparent_invalid():
    """Invalid traceparent returns None."""
    assert extract_traceparent("invalid") is None
    assert extract_traceparent("") is None
    assert extract_traceparent("00-00000000000000000000000000000000-b7ad6b7169203331-01") is None


def test_inject_headers_with_llm_marker():
    """inject_headers includes ownership marker for logical LLM spans."""
    ctx = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=generate_span_id(),
        span_kind="LLM",
        sampled=True,
        logical_llm_span_active=True,
    )
    headers = inject_headers(ctx, is_logical_llm=True)
    assert "traceparent" in headers
    assert headers["X-LLM-OBS-Span-Role"] == "llm"


def test_inject_headers_without_llm_marker():
    """inject_headers omits ownership marker for non-LLM spans."""
    ctx = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=None,
        span_kind="AGENT",
        sampled=True,
    )
    headers = inject_headers(ctx, is_logical_llm=False)
    assert "traceparent" in headers
    assert "X-LLM-OBS-Span-Role" not in headers


def test_inject_headers_includes_metadata():
    """inject_headers includes session/user/app metadata."""
    ctx = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=None,
        span_kind="AGENT",
        sampled=True,
    )
    headers = inject_headers(
        ctx,
        is_logical_llm=False,
        session_id="sess-1",
        user_id="user-1",
        app_name="my-app",
    )
    assert headers["X-Session-Id"] == "sess-1"
    assert headers["X-User-Id"] == "user-1"
    assert headers["X-App-Name"] == "my-app"
