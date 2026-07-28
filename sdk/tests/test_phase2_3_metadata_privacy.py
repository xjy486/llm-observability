"""P0-3: Metadata privacy and sanitization tests."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from llm_observability.integrations.langchain.metadata import sanitize_langchain_config_metadata


def test_langchain_metadata_sensitive_keys_are_masked():
    config = {"metadata": {"api_key": "sk-secret123", "authorization": "Bearer xxx"}}
    result = sanitize_langchain_config_metadata(config, "masked")
    md = result.get("langchain.metadata", {})
    assert md["api_key"] == "***REDACTED***"
    assert md["authorization"] == "***REDACTED***"


def test_langchain_metadata_sensitive_text_is_masked():
    config = {"metadata": {"note": "my key is sk-abcdefghij1234567890abcd"}}
    result = sanitize_langchain_config_metadata(config, "masked")
    md = result.get("langchain.metadata", {})
    assert "sk-***REDACTED***" in md["note"]


def test_langchain_metadata_custom_object_is_json_safe():
    class CustomObj:
        pass
    config = {"metadata": {"client": CustomObj()}}
    result = sanitize_langchain_config_metadata(config, "masked")
    md = result.get("langchain.metadata", {})
    # Must be JSON-serializable
    import json
    json.dumps(md)
    assert "CustomObj" in str(md["client"])


def test_langchain_metadata_over_16k_is_truncated():
    big = "x" * (20 * 1024)
    config = {"metadata": {"big_field": big}}
    result = sanitize_langchain_config_metadata(config, "masked")
    import json
    total = len(json.dumps(result).encode("utf-8"))
    assert total <= 16 * 1024 + 512  # bounded


def test_langchain_tags_sensitive_text_is_masked():
    config = {"tags": ["normal", "token=secret123"]}
    result = sanitize_langchain_config_metadata(config, "masked")
    tags = result.get("langchain.tags", [])
    assert "token=***REDACTED***" in tags
    assert "normal" in tags


def test_langchain_config_does_not_poison_agent_record():
    """Result must be JSON-serializable."""
    config = {
        "configurable": {"thread_id": "t1"},
        "run_name": "test",
        "tags": ["a", "b"],
        "metadata": {"nested": {"deep": {"value": 42}}},
    }
    result = sanitize_langchain_config_metadata(config, "masked")
    import json
    json.dumps(result)  # must not raise
