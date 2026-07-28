"""P1: thread_id and run_name masking tests.

Verifies that sensitive text patterns in thread_id and run_name are
masked through _mask_string_patterns before being set as span attributes.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from llm_observability.integrations.langchain.metadata import sanitize_langchain_config_metadata


def test_langchain_thread_id_sensitive_text_is_masked():
    """thread_id containing sensitive patterns like 'Bearer xxx' must be masked."""
    config = {"configurable": {"thread_id": "Bearer abcdef1234567890"}}
    result = sanitize_langchain_config_metadata(config, "masked")
    tid = result.get("langchain.thread_id", "")
    assert "Bearer" not in tid or "***REDACTED***" in tid, (
        f"thread_id sensitive text not masked: {tid}"
    )


def test_langchain_thread_id_api_key_pattern_is_masked():
    """thread_id containing 'sk-xxx' pattern must be masked."""
    config = {"configurable": {"thread_id": "sk-abcdefghij1234567890abcd"}}
    result = sanitize_langchain_config_metadata(config, "masked")
    tid = result.get("langchain.thread_id", "")
    assert "sk-***REDACTED***" in tid or "***REDACTED***" in tid, (
        f"thread_id API key pattern not masked: {tid}"
    )


def test_langchain_thread_id_token_pattern_is_masked():
    """thread_id containing 'token=xxx' pattern must be masked."""
    config = {"configurable": {"thread_id": "token=secret1234567890abcd"}}
    result = sanitize_langchain_config_metadata(config, "masked")
    tid = result.get("langchain.thread_id", "")
    assert "token=***REDACTED***" in tid or "***REDACTED***" in tid, (
        f"thread_id token pattern not masked: {tid}"
    )


def test_langchain_run_name_sensitive_text_is_masked():
    """run_name containing sensitive patterns must be masked."""
    config = {"run_name": "token=secret1234567890abcd"}
    result = sanitize_langchain_config_metadata(config, "masked")
    rn = result.get("langchain.run_name", "")
    assert "token=***REDACTED***" in rn or "***REDACTED***" in rn, (
        f"run_name token pattern not masked: {rn}"
    )


def test_langchain_run_name_api_key_pattern_is_masked():
    """run_name containing 'sk-xxx' pattern must be masked."""
    config = {"run_name": "key=sk-abcdefghij1234567890abcd"}
    result = sanitize_langchain_config_metadata(config, "masked")
    rn = result.get("langchain.run_name", "")
    assert "sk-***REDACTED***" in rn or "***REDACTED***" in rn, (
        f"run_name API key pattern not masked: {rn}"
    )


def test_langchain_thread_id_normal_value_preserved():
    """Normal thread_id without sensitive patterns is preserved (truncated only)."""
    config = {"configurable": {"thread_id": "thread-12345"}}
    result = sanitize_langchain_config_metadata(config, "masked")
    tid = result.get("langchain.thread_id", "")
    assert tid == "thread-12345", f"Normal thread_id should be preserved, got: {tid}"


def test_langchain_run_name_normal_value_preserved():
    """Normal run_name without sensitive patterns is preserved."""
    config = {"run_name": "my-agent-run"}
    result = sanitize_langchain_config_metadata(config, "masked")
    rn = result.get("langchain.run_name", "")
    assert rn == "my-agent-run", f"Normal run_name should be preserved, got: {rn}"
