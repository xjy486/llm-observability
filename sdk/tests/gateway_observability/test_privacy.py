"""PrivacyGuard tests (spec §16)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import PrivacyGuard, GatewayRuntime
from llm_observability.gateway_observability.errors import safe_error_message


def _guard(secret="secret-key"):
    return PrivacyGuard(secret=secret)


def test_secret_attributes_blocked():
    g = _guard()
    sanitized = g.sanitize_attributes({
        "authorization": "Bearer sk-secret123",
        "api_key": "sk-abc123",
        "cookie": "session=abc",
        "set-cookie": "sid=xyz",
        "provider_secret": "raw-secret",
        "full_prompt": "the full prompt text",
        "full_response": "the full response",
        "tool_input": "user data",
        "uploaded_file": "file-bytes",
    })
    # None of these keys survive.
    assert sanitized == {}


def test_allowed_attributes_kept():
    g = _guard()
    sanitized = g.sanitize_attributes({
        "provider": "openai",
        "model": "gpt-5.6",
        "status": "OK",
        "error_category": "rate_limit",
        "request_id": "req-123",
        "usage.input_tokens": 5,
        "cost.total": 0.5,
    })
    assert sanitized["provider"] == "openai"
    assert sanitized["model"] == "gpt-5.6"
    assert sanitized["error_category"] == "rate_limit"
    assert sanitized["usage.input_tokens"] == 5
    assert sanitized["cost.total"] == 0.5


def test_channel_id_hashed():
    g = _guard()
    hashed = g.hash_channel_id("channel-12")
    assert hashed is not None
    assert hashed != "channel-12"
    # Deterministic
    assert g.hash_channel_id("channel-12") == hashed
    assert len(hashed) == 16


def test_channel_id_hash_without_secret():
    g = PrivacyGuard()  # no secret → SHA-256 digest
    hashed = g.hash_channel_id("ch-1")
    assert hashed is not None and hashed != "ch-1"
    assert len(hashed) == 16


def test_secret_patterns_masked_in_string():
    g = _guard()
    assert g.sanitize_string("key is sk-abcDEF1234567890xyz") == "key is <redacted>"
    assert g.sanitize_string("Authorization: Bearer abc123token==") != "Authorization: Bearer abc123token=="
    assert "<redacted>" in g.sanitize_string("Authorization: Bearer abc123token==")


def test_url_query_stripped():
    g = _guard()
    sanitized = g.sanitize_url("https://api.example.com/v1/chat?api_key=secret&foo=bar")
    assert "api_key" not in sanitized
    assert sanitized.startswith("https://api.example.com/v1/chat")


def test_fail_closed_masking():
    g = _guard()
    # Non-serializable value → redacted, span still proceeds
    assert g.sanitize_value(object()) == "<redacted>"
    # Sanitization of poisoned input does not raise
    class Poison:
        def items(self):
            raise RuntimeError("boom")
    assert g.sanitize_attributes(Poison()) == {}  # type: ignore


def test_end_to_end_no_secrets_in_telemetry(clean_sdk):
    """Scenario J: no secrets in any span/event/log."""
    rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    router = handle.router
    a = handle.start_attempt({"attempt_index": 1, "channel_id": "channel-secret-9"})
    a.start()
    handle.finish_attempt(a, upstream_status=200,
                         raw_usage={"prompt_tokens": 1, "completion_tokens": 1})
    a.close()
    handle.finalize()

    record = router.span.to_record()
    text = str(record)
    assert "channel-secret-9" not in text
    assert "sk-" not in text
    # Channel appears only in hashed form
    assert "gateway.channel_id" in text
