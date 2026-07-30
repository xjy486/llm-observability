"""Phase 2.5 — Proxy control-character sanitization (P1-4).

Verifies that _proxy_sanitize_value strips ALL control characters (including
CR/LF/tab) and that extract_metadata_headers sanitizes X-* header values
to prevent log/header injection via %0A-decoded newlines.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "proxy"))

import pytest

from trace_context import (
    _proxy_sanitize_value,
    extract_metadata_headers,
    _proxy_parse_association_baggage,
)


# ── _proxy_sanitize_value strips all control characters ──

def test_sanitize_strips_newline():
    assert "\n" not in _proxy_sanitize_value("hello\nworld")


def test_sanitize_strips_carriage_return():
    assert "\r" not in _proxy_sanitize_value("hello\rworld")


def test_sanitize_strips_tab():
    assert "\t" not in _proxy_sanitize_value("hello\tworld")


def test_sanitize_strips_multiple_control_chars():
    raw = "a\nb\rc\td\x00e\x1ff"
    result = _proxy_sanitize_value(raw)
    for ch in ("\n", "\r", "\t", "\x00", "\x1f"):
        assert ch not in result, f"control char {repr(ch)} survived"


def test_sanitize_preserves_printable():
    assert _proxy_sanitize_value("hello world") == "hello world"
    assert _proxy_sanitize_value("user-123") == "user-123"
    assert _proxy_sanitize_value("a@b.com") == "a@b.com"


def test_sanitize_truncates():
    long = "x" * 500
    result = _proxy_sanitize_value(long, max_length=256)
    assert len(result) == 256


def test_sanitize_none_returns_none():
    assert _proxy_sanitize_value(None) is None


# ── extract_metadata_headers sanitizes X-* header values ──

def test_x_user_id_strips_control_chars():
    """X-User-Id with embedded newline is sanitized."""
    headers = {"X-User-Id": "alice\nMalicious-Header: injected"}
    meta = extract_metadata_headers(headers)
    assert "\n" not in meta["user_id"]
    assert "alice" in meta["user_id"]


def test_x_session_id_strips_control_chars():
    headers = {"X-Session-Id": "s1\r\nX-Injected: evil"}
    meta = extract_metadata_headers(headers)
    assert "\r" not in meta["session_id"]
    assert "\n" not in meta["session_id"]


def test_x_app_name_strips_tab():
    headers = {"X-App-Name": "app\tname"}
    meta = extract_metadata_headers(headers)
    assert "\t" not in meta["app_name"]


def test_x_business_scene_strips_control():
    headers = {"X-Business-Scene": "scene\n\r\tdata"}
    meta = extract_metadata_headers(headers)
    for ch in ("\n", "\r", "\t"):
        assert ch not in meta["business_scene"]


def test_x_trace_attr_strips_control():
    headers = {"X-Trace-Custom": "val\ninjected"}
    meta = extract_metadata_headers(headers)
    assert "\n" not in meta["attr_custom"]


def test_all_x_headers_sanitized_consistently():
    """All X-* header values go through the same sanitizer."""
    headers = {
        "X-User-Id": "u\n",
        "X-Session-Id": "s\n",
        "X-App-Name": "a\n",
        "X-Business-Scene": "b\n",
        "X-Trace-Key": "v\n",
    }
    meta = extract_metadata_headers(headers)
    for v in meta.values():
        assert "\n" not in v, f"control char survived in {v}"


# ── Baggage header also sanitized (regression) ──

def test_baggage_value_strips_control_chars():
    """Baggage header values are sanitized (regression — was already fixed)."""
    baggage = "user=alice%0Ainjected,session_id=s1"
    meta = _proxy_parse_association_baggage(baggage)
    assert "\n" not in meta.get("user_id", "")
    assert "alice" in meta.get("user_id", "")


def test_baggage_value_strips_decoded_newline():
    """%0A decoded to \\n is stripped by the sanitizer."""
    from urllib.parse import quote
    malicious = "alice\nMalicious-Header: injected"
    baggage = f"user={quote(malicious)}"
    meta = _proxy_parse_association_baggage(baggage)
    assert "\n" not in meta.get("user_id", "")


# ── X-* headers take precedence over baggage ──

def test_x_header_overrides_baggage():
    """Compat headers (X-*) take precedence over baggage."""
    headers = {
        "baggage": "user=from_baggage",
        "X-User-Id": "from_xheader",
    }
    meta = extract_metadata_headers(headers)
    assert meta["user_id"] == "from_xheader"
