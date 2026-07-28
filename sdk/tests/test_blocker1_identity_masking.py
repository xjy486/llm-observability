"""Blocker 1: Identity field sanitization tests.

Verifies that session_id, user_id, business_scene, and their callable/
config-derived values are all masked through _mask_string_patterns,
not just the span attributes langchain.thread_id / langchain.run_name.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import _context_var
from llm_observability.integrations.langchain.agent_wrapper import (
    observe_agent,
    _sanitize_identity_value,
    _resolve_session_id,
    _resolve_user_id,
    _resolve_business_scene,
)


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    if _context_var.get() is not None:
        _context_var.set(None)
    Observability.init(app_name="identity-mask-test", endpoint="http://localhost:99999")
    yield Observability._tracer
    Observability.shutdown()


# ── Unit tests for _sanitize_identity_value ──

def test_sanitize_identity_value_masks_bearer():
    result = _sanitize_identity_value("Bearer abcdef1234567890")
    assert "abcdef" not in result
    assert "REDACTED" in result or result != "Bearer abcdef1234567890"


def test_sanitize_identity_value_masks_sk_prefix():
    result = _sanitize_identity_value("sk-abcdefghij1234567890abcd")
    assert "REDACTED" in result or "abcdefghij" not in result


def test_sanitize_identity_value_masks_token_pattern():
    result = _sanitize_identity_value("token=secret1234567890abcd")
    assert "REDACTED" in result or "secret123" not in result


def test_sanitize_identity_value_preserves_normal():
    result = _sanitize_identity_value("thread-12345")
    assert result == "thread-12345"


def test_sanitize_identity_value_truncates():
    long_val = "x" * 300
    result = _sanitize_identity_value(long_val, max_length=256)
    assert len(result) == 256


def test_sanitize_identity_value_handles_none():
    assert _sanitize_identity_value(None) is None


# ── Integration: thread_id → session_id mapping ──

def test_sensitive_thread_id_masked_in_resolve_session_id():
    """thread_id with Bearer pattern is masked when mapped to session_id."""
    config = {"configurable": {"thread_id": "Bearer very-secret-token-12345678"}}
    result = _resolve_session_id(None, None, config)
    assert result is not None
    assert "very-secret" not in result, f"session_id leaked sensitive text: {result}"
    assert "REDACTED" in result or "Bearer" not in result


def test_sensitive_thread_id_masked_in_resolve_session_id_sk_pattern():
    """thread_id with sk- pattern is masked when mapped to session_id."""
    config = {"configurable": {"thread_id": "sk-abcdefghij1234567890abcd"}}
    result = _resolve_session_id(None, None, config)
    assert "abcdefghij" not in result, f"session_id leaked API key: {result}"


def test_sensitive_callable_session_id_is_masked(init_sdk):
    """Callable returning sensitive text is masked before reaching trace."""
    def get_session(input, config):
        return "Bearer callable-secret-token-123456789"

    result = _resolve_session_id(get_session, None, None)
    assert "callable-secret" not in result, f"Callable session_id leaked: {result}"
    assert "REDACTED" in result or "Bearer" not in result


def test_sensitive_config_user_id_is_masked(init_sdk):
    """config.metadata.user_id with sensitive pattern is masked."""
    config = {"metadata": {"user_id": "token=secret-user-token-123456789"}}
    result = _resolve_user_id(None, None, config)
    assert result is not None
    assert "secret-user-token" not in result, f"user_id leaked: {result}"
    assert "REDACTED" in result


def test_sensitive_config_business_scene_is_masked(init_sdk):
    """config.metadata.business_scene with sensitive pattern is masked."""
    config = {"metadata": {"business_scene": "Bearer biz-secret-token-123456789"}}
    result = _resolve_business_scene(None, None, config)
    assert result is not None
    assert "biz-secret-token" not in result, f"business_scene leaked: {result}"
    assert "REDACTED" in result


# ── Full trace integration: session_id appears in trace record ──

def test_sensitive_thread_id_masked_in_trace_session_id(init_sdk):
    """Full trace: session_id in the reported span record is masked.

    This is the core Blocker 1 test: thread_id="Bearer xxx" flows through
    _resolve_session_id → Observability.trace(session_id=...) → Span →
    to_record(). The top-level session_id must NOT contain the raw secret.
    """
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(return_value={"result": "ok"})

    observed = observe_agent(
        fake_agent,
        name="leak-test-agent",
        root_mode="create",
    )

    config = {"configurable": {"thread_id": "Bearer very-secret-token-123456789"}}
    result = observed.invoke({"messages": []}, config=config)

    tracer.reporter.report = orig
    assert result == {"result": "ok"}

    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    span = agent_spans[0]

    # Top-level session_id must be masked
    session_id = span.get("session_id", "")
    assert "very-secret" not in session_id, (
        f"Top-level session_id leaked sensitive text: {session_id}"
    )
    assert "REDACTED" in session_id or "Bearer" not in session_id, (
        f"session_id not properly masked: {session_id}"
    )

    # Also verify langchain.thread_id attribute is masked (should match)
    attrs = span.get("attributes", {})
    thread_id_attr = attrs.get("langchain.thread_id", "")
    assert "very-secret" not in thread_id_attr, (
        f"langchain.thread_id attribute leaked: {thread_id_attr}"
    )

    # Both should come from the same sanitized result
    assert session_id == thread_id_attr, (
        f"session_id ({session_id}) != langchain.thread_id ({thread_id_attr}) — "
        "they should share the same sanitized value"
    )