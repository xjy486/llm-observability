"""Cache + rate-limit semantics tests (spec §14)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import GatewayRuntime, PrivacyGuard, ErrorCategory
from llm_observability.gateway_observability.events import (
    EVENT_CACHE_HIT,
    EVENT_RATE_LIMIT_REJECTED,
)


def _handle():
    return GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s")).handle_request(
        {"gateway_name": "mock"}
    )


def test_cache_hit_creates_no_attempt(clean_sdk):
    handle = _handle()
    router = handle.router
    handle.cache_hit(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    handle.finalize()

    assert router.span.attributes["gateway.cache_status"] == "hit"
    assert router.span.attributes["gateway.attempt_count"] == 0
    assert len(router.attempts) == 0
    assert router.span.status == "OK"
    assert router.span.attributes["usage.input_tokens"] == 10
    # No Attempt span under the Router
    assert [e for e in router.span.events if e["name"] == EVENT_CACHE_HIT]


def test_cache_miss_creates_attempt(clean_sdk):
    handle = _handle()
    router = handle.router
    handle.cache_miss()
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)
    a.close()
    handle.finalize()
    assert router.span.attributes["gateway.cache_status"] == "miss"
    assert router.span.attributes["gateway.attempt_count"] == 1


def test_rate_limit_rejection_no_attempt(clean_sdk):
    handle = _handle()
    router = handle.router
    handle.rate_limited()
    handle.finalize()

    assert router.span.status == "ERROR"
    assert router.span.attributes["gateway.attempt_count"] == 0
    assert len(router.attempts) == 0
    assert router.final_error.category == ErrorCategory.RATE_LIMIT
    assert [e for e in router.span.events if e["name"] == EVENT_RATE_LIMIT_REJECTED]
    # Router error category attribute present
    assert router.span.attributes["gateway.final_error_category"] == ErrorCategory.RATE_LIMIT


def test_rate_limit_no_fake_attempt(clean_sdk):
    """No upstream request → no fake Attempt (spec §14.2)."""
    handle = _handle()
    router = handle.router
    handle.rate_limited()
    assert len(router.attempts) == 0
    handle.finalize()
    # Context/registry clean
    from llm_observability.gateway_observability.context import GatewayContext
    state = GatewayContext.get()
    assert state.router is None
