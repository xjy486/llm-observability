"""One-API adapter tests (spec §19)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.integrations.oneapi import OneApiAdapter
from llm_observability.integrations.oneapi.request_mapper import map_request_token
from llm_observability.integrations.oneapi.channel_mapper import (
    map_channel,
    map_model_mapping,
    map_relay_mode,
)
from llm_observability.integrations.oneapi.retry_mapper import (
    map_retry_state,
    map_fallback_state,
)
from llm_observability.gateway_observability import GatewayRuntime, PrivacyGuard
from llm_observability.gateway_observability.errors import ErrorCategory


def _state(**kwargs):
    base = {
        "provider": "openai",
        "channel_id": "ch-12",
        "channel_type": "azure",
        "requested_model": "gpt-5.6",
        "resolved_model": "gpt-5.6-azure",
        "route_reason": "weighted-random",
        "cache_status": None,
        "rate_limited": False,
    }
    base.update(kwargs)
    return base


def test_request_token_mapping():
    token = {"user_id": "u1", "session_id": "s1", "model": "gpt-5.6", "app": "my-app"}
    rc = map_request_token(token)
    assert rc.user_id == "u1"
    assert rc.session_id == "s1"
    assert rc.requested_model == "gpt-5.6"
    assert rc.app_name == "my-app"


def test_channel_mapping():
    channel = {"id": 12, "type": "azure", "provider": "openai"}
    mapped = map_channel(channel)
    assert mapped["channel_id"] == 12
    assert mapped["channel_type"] == "azure"
    assert mapped["provider"] == "openai"


def test_model_mapping():
    mapping = {"gpt-5.6": "azure-gpt-5.6"}
    mapped = map_model_mapping(mapping, requested_model="gpt-5.6")
    assert mapped["resolved_model"] == "azure-gpt-5.6"


def test_relay_mode_mapping():
    assert map_relay_mode("chat") == "openai-compatible"
    assert map_relay_mode("anthropic") == "anthropic"


def test_adapter_request_context():
    adapter = OneApiAdapter(gateway_name="one-api")
    rc = adapter.extract_request_context({
        "token": {"user_id": "u1", "model": "gpt-5.6"},
    })
    assert rc.gateway_name == "one-api"
    assert rc.user_id == "u1"


def test_adapter_route_decision():
    adapter = OneApiAdapter()
    rd = adapter.extract_route_decision(_state())
    assert rd is not None
    assert rd.provider == "openai"
    assert rd.channel_id == "ch-12"
    assert rd.resolved_model == "gpt-5.6-azure"
    assert rd.route_reason == "weighted-random"


def test_adapter_attempt_context():
    adapter = OneApiAdapter()
    ctx = adapter.extract_attempt_context({
        "attempt_index": 2, "channel_id": "ch-12", "timeout_ms": 5000,
    })
    assert ctx.attempt_index == 2
    assert ctx.timeout_ms == 5000


def test_adapter_usage_extraction():
    adapter = OneApiAdapter()
    usage = adapter.extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    assert usage["prompt_tokens"] == 10


def test_adapter_classifies_error():
    adapter = OneApiAdapter()
    err = adapter.classify_error(TimeoutError("timed out"))
    assert err.category == ErrorCategory.TIMEOUT
    assert err.retryable is True


def test_retry_mapper():
    state = {"attempt_index": 1, "retry_delay_ms": 200, "retry_reason": "provider_5xx"}
    mapped = map_retry_state(state)
    assert mapped["attempt_index"] == 2
    assert mapped["delay_ms"] == 200
    assert mapped["reason"] == "provider_5xx"


def test_fallback_mapper():
    state = {"from_channel_id": "ch-a", "fallback_channel_id": "ch-b", "fallback_reason": "timeout"}
    mapped = map_fallback_state(state)
    assert mapped["from_channel_id"] == "ch-a"
    assert mapped["to_channel_id"] == "ch-b"


def test_adapter_end_to_end_retry_via_runtime(clean_sdk):
    """Scenario B shape: One-API adapter drives 500→200 through the runtime."""
    adapter = OneApiAdapter(gateway_name="one-api")
    rt = GatewayRuntime(
        adapter=adapter,
        sample_rate=1.0,
        privacy=PrivacyGuard(secret="s"),
    )
    handle = rt.handle_request({
        "token": {"user_id": "u1", "model": "gpt-5.6"},
        "channel": {"id": 12, "type": "azure", "provider": "openai"},
        "model_mapping": {"gpt-5.6": "azure-gpt-5.6"},
    })
    router = handle.router

    # Attempt 1 → 500
    a1 = handle.start_attempt({"attempt_index": 1})
    a1.start()
    handle.finish_attempt(a1, upstream_status=500)
    a1.close()
    handle.retry_scheduled(attempt_index=1, delay_ms=200, reason="provider_5xx")

    # Attempt 2 → 200
    a2 = handle.start_attempt({"attempt_index": 2})
    a2.start()
    handle.finish_attempt(a2, upstream_status=200,
                         raw_usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})
    a2.close()
    handle.finalize()

    assert router.span.attributes["gateway.retry_count"] == 1
    assert router.span.attributes["gateway.attempt_count"] == 2
    assert router.span.status == "OK"
    # Channel hashed (privacy), provider mapped
    assert router.span.attributes["gateway.provider"] == "openai"
    assert len(router.span.attributes["gateway.channel_id"]) == 16
