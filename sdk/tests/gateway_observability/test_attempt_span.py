"""Attempt span lifecycle + attributes tests (spec §4, §9.3)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    GatewayError,
    ErrorCategory,
)
from llm_observability.gateway_observability.registry import AttemptRegistry
from llm_observability.gateway_observability.context import GatewayContext


def _handle(rt=None):
    rt = rt or GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    return rt.handle_request({"gateway_name": "mock"})


def test_attempt_role_and_index(clean_sdk):
    handle = _handle()
    a = handle.start_attempt({"attempt_index": 1, "provider": "openai", "channel_id": "ch-1"})
    a.start()
    assert a.span.attributes["gateway.span_role"] == "provider_attempt"
    assert a.span.attributes["gateway.attempt_index"] == 1
    assert a.span.attributes["gateway.provider"] == "openai"
    # Channel hashed
    assert a.span.attributes["gateway.channel_id"] != "ch-1"
    assert len(a.span.attributes["gateway.channel_id"]) == 16
    a.close()
    handle.finalize()


def test_attempt_parent_is_router(clean_sdk):
    handle = _handle()
    router = handle.router
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    assert a.span.parent_span_id == router.span.span_id
    assert a.span.trace_id == router.span.trace_id
    a.close()
    handle.finalize()


def test_attempt_http_500_classified_provider_5xx_retryable(clean_sdk):
    handle = _handle()
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=500, duration_ms=42.0)
    a.close()
    assert a.span.attributes["gateway.error_category"] == ErrorCategory.PROVIDER_5XX
    assert a.span.attributes["gateway.retryable"] is True
    assert a.span.status == "ERROR"
    assert a.span.attributes["gateway.upstream_duration_ms"] == 42.0
    handle.finalize()


def test_attempt_http_429_rate_limit_not_retryable_business(clean_sdk):
    handle = _handle()
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=429)
    a.close()
    assert a.span.attributes["gateway.error_category"] == ErrorCategory.RATE_LIMIT
    handle.finalize()


def test_attempt_usage_and_cost_recorded(clean_sdk):
    handle = _handle()
    a = handle.start_attempt({"attempt_index": 1, "resolved_model": "gpt-5.6"})
    a.start()
    handle.finish_attempt(a, upstream_status=200,
                         raw_usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14})
    a.close()
    assert a.span.attributes["usage.input_tokens"] == 10
    assert a.span.attributes["usage.output_tokens"] == 4
    assert a.span.attributes["cost.source"] == "unpriced"
    handle.finalize()


def test_attempt_finish_reason_and_ttft(clean_sdk):
    handle = _handle()
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200, ttft_ms=88.0, finish_reason="stop")
    a.close()
    assert a.span.attributes["gateway.upstream_ttft_ms"] == 88.0
    assert a.span.attributes["gateway.finish_reason"] == "stop"
    handle.finalize()


def test_attempt_error_exception_classified(clean_sdk):
    handle = _handle()
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, error=TimeoutError("upstream hung"))
    a.close()
    assert a.span.attributes["gateway.error_category"] == ErrorCategory.TIMEOUT
    assert a.span.attributes["gateway.retryable"] is True
    assert "Authorization" not in a.span.to_record().get("attributes", {})
    handle.finalize()


def test_attempt_registry_cleanup(clean_sdk):
    reg = AttemptRegistry()
    handle = _handle(GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"),
                                    attempt_registry=reg))
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    assert reg.size() == 1
    a.close()
    assert reg.size() == 0
    # active attempt cleared
    state = GatewayContext.get()
    assert state.active_attempt is None
    handle.finalize()


def test_attempt_active_in_context(clean_sdk):
    handle = _handle()
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    state = GatewayContext.get()
    assert state.active_attempt is not None
    assert state.active_attempt.attempt() is a
    assert GatewayContext.active_attempt() is a
    a.close()
    state = GatewayContext.get()
    assert state.active_attempt is None
    handle.finalize()


def test_fresh_attempt_never_reused(clean_sdk):
    """Each retry creates a fresh Attempt span (spec §4.2)."""
    handle = _handle()
    a1 = handle.start_attempt({"attempt_index": 1})
    a1.start()
    handle.finish_attempt(a1, upstream_status=500)
    a1.close()

    a2 = handle.start_attempt({"attempt_index": 2})
    a2.start()
    assert a2 is not a1
    assert a2.span.span_id != a1.span.span_id
    handle.finish_attempt(a2, upstream_status=200)
    a2.close()
    assert a2.span.attributes["gateway.attempt_index"] == 2
    handle.finalize()
