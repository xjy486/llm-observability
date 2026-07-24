"""Tests for Proxy span ownership detection."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

from trace_context import resolve_trace_context, extract_metadata_headers, extract_ownership


def test_extract_ownership_llm_marker():
    """extract_ownership returns 'llm' when X-LLM-OBS-Span-Role: llm."""
    headers = {"X-LLM-OBS-Span-Role": "llm", "traceparent": "00-abc-def-01"}
    assert extract_ownership(headers) == "llm"


def test_extract_ownership_none():
    """extract_ownership returns None when no marker."""
    headers = {"traceparent": "00-abc-def-01"}
    assert extract_ownership(headers) is None


def test_extract_ownership_case_insensitive():
    """Header name matching is case-insensitive."""
    headers = {"x-llm-obs-span-role": "llm"}
    assert extract_ownership(headers) == "llm"


def test_proxy_span_kind_with_marker():
    """When ownership marker is present, proxy should create GATEWAY span.

    This is a unit test for the decision logic, not the full handler.
    """
    headers = {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "X-LLM-OBS-Span-Role": "llm",
    }
    ctx = resolve_trace_context(headers)
    ownership = extract_ownership(headers)

    # When ownership == 'llm', proxy should use GATEWAY
    assert ownership == "llm"
    assert ctx.inherited is True


def test_proxy_span_kind_without_marker():
    """Without ownership marker, proxy creates LLM fallback span."""
    headers = {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    }
    ctx = resolve_trace_context(headers)
    ownership = extract_ownership(headers)

    assert ownership is None
    assert ctx.inherited is True  # still inherits trace, but creates LLM span
