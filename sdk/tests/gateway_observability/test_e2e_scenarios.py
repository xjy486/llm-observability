"""Mock gateway harness + E2E scenarios (spec §23, task 8.2).

Drives the gateway runtime directly (not through One-API) to prove the
acceptance scenarios:

  A success, B retry (500→200), C fallback (timeout→success), D cache hit,
  E rate limit, F streaming success, G streaming cancel, H no-SDK root,
  I sampling=0, J privacy.

Business primacy: every scenario returns the business result unchanged even
though telemetry is fully exercised.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import (
    GatewayRuntime,
    GatewayStream,
    AsyncGatewayStream,
    PrivacyGuard,
    ErrorCategory,
)
from llm_observability.gateway_observability.context import GatewayContext
from llm_observability.gateway_observability.events import (
    EVENT_STREAM_COMPLETED,
    EVENT_STREAM_CANCELLED,
    EVENT_FALLBACK_SELECTED,
)


def _runtime(**kwargs):
    kwargs.setdefault("sample_rate", 1.0)
    kwargs.setdefault("privacy", PrivacyGuard(secret="e2e-secret"))
    return GatewayRuntime(**kwargs)


# ── Scenario A: success ──

def test_scenario_a_success(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    router = handle.router
    a = handle.start_attempt({"attempt_index": 1, "provider": "openai", "channel_id": "ch-1"})
    a.start()
    handle.finish_attempt(a, upstream_status=200, duration_ms=120.0,
                         raw_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    a.close()
    handle.finalize()

    assert router.span.status == "OK"
    assert router.span.attributes["gateway.attempt_count"] == 1
    assert router.span.attributes["usage.total_tokens"] == 15
    # 1 AGENT-ish root (Router) + 1 Attempt — but no fabricated LLM/AGENT spans.
    assert router.span.span_kind == "GATEWAY"
    assert router.span.parent_span_id is None  # Router is root here


# ── Scenario B: retry 500→200 ──

def test_scenario_b_retry(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    router = handle.router

    a1 = handle.start_attempt({"attempt_index": 1, "channel_id": "ch-1"})
    a1.start()
    handle.finish_attempt(a1, upstream_status=500)
    a1.close()
    handle.retry_scheduled(attempt_index=1, delay_ms=100, reason="provider_5xx")

    a2 = handle.start_attempt({"attempt_index": 2, "channel_id": "ch-1"})
    a2.start()
    handle.finish_attempt(a2, upstream_status=200,
                         raw_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    a2.close()
    handle.finalize()

    assert router.span.attributes["gateway.attempt_count"] == 2
    assert router.span.attributes["gateway.retry_count"] == 1
    assert router.span.status == "OK"
    assert router.attempts[0].span.attributes["gateway.error_category"] == ErrorCategory.PROVIDER_5XX
    assert router.attempts[1].span.status == "OK"


# ── Scenario C: fallback (timeout→success) ──

def test_scenario_c_fallback(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    router = handle.router

    a1 = handle.start_attempt({"attempt_index": 1, "channel_id": "ch-a"})
    a1.start()
    handle.finish_attempt(a1, error=TimeoutError("timed out"))
    a1.close()
    handle.fallback_selected(from_channel_id="ch-a", to_channel_id="ch-b", reason="timeout")

    a2 = handle.start_attempt({"attempt_index": 2, "channel_id": "ch-b"})
    a2.start()
    handle.finish_attempt(a2, upstream_status=200)
    a2.close()
    handle.finalize()

    fallback_events = [e for e in router.span.events if e["name"] == EVENT_FALLBACK_SELECTED]
    assert len(fallback_events) == 1
    assert router.span.attributes["gateway.fallback_count"] == 1
    assert router.final_channel_id == "ch-b"
    assert router.span.status == "OK"


# ── Scenario D: cache hit ──

def test_scenario_d_cache_hit(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    router = handle.router
    handle.cache_hit(usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10})
    handle.finalize()

    assert router.span.attributes["gateway.cache_status"] == "hit"
    assert router.span.attributes["gateway.attempt_count"] == 0
    assert len(router.attempts) == 0


# ── Scenario E: rate limit ──

def test_scenario_e_rate_limit(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock"})
    router = handle.router
    handle.rate_limited()
    handle.finalize()

    assert router.span.status == "ERROR"
    assert router.span.attributes["gateway.attempt_count"] == 0
    assert router.final_error.category == ErrorCategory.RATE_LIMIT


# ── Scenario F: streaming success ──

def test_scenario_f_streaming_success(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    router = handle.router
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)

    stream = GatewayStream(iter(["a", "b", "[DONE]"]), router, a, runtime_handle=handle)
    list(stream)

    first_token = [e for e in router.span.events if e["name"] == "gateway.stream.first_token"]
    assert len(first_token) == 1
    assert router.span.attributes["gateway.ttft_ms"] is not None
    assert [e for e in router.span.events if e["name"] == EVENT_STREAM_COMPLETED]
    assert router.span.end_time > 0


# ── Scenario G: streaming cancel ──

def test_scenario_g_streaming_cancel(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock"})
    router = handle.router
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)

    stream = GatewayStream(iter(["a", "b"]), router, a, runtime_handle=handle)
    next(iter(stream))
    stream.close()

    assert [e for e in router.span.events if e["name"] == EVENT_STREAM_CANCELLED]
    assert a.span.attributes["gateway.error_category"] == ErrorCategory.CLIENT_CANCELLED
    assert router.span.end_time > 0
    state = GatewayContext.get()
    assert state.router is None and state.active_attempt is None


# ── Scenario H: no-SDK root ──

def test_scenario_h_no_sdk_root(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock"})
    router = handle.router
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)
    a.close()
    handle.finalize()

    assert router.span.parent_span_id is None  # Router is Root
    assert router.span.attributes["gateway.trace_origin"] == "gateway"
    assert router.span.attributes["gateway.upstream_trace_present"] is False
    # No fabricated LLM/AGENT spans — only GATEWAY kind spans in the trace.
    assert router.span.span_kind == "GATEWAY"
    assert a.span.span_kind == "GATEWAY"


# ── Scenario I: sampling=0 ──

def test_scenario_i_sampling_zero(clean_sdk):
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"  # sampled out
    rt = _runtime(tracer=clean_sdk, sample_rate=0.0)
    handle = rt.handle_request({"gateway_name": "mock"}, upstream_traceparent=tp)
    router = handle.router
    # Business still runs
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)
    a.close()
    handle.finalize()

    assert router.span is not None
    # No Reporter Record
    assert len(clean_sdk.reporter._queue) == 0
    # Upstream traceparent propagated (trace_id inherited)
    assert router.span.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"


# ── Scenario J: privacy ──

def test_scenario_j_privacy(clean_sdk):
    rt = _runtime(tracer=clean_sdk)
    handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    router = handle.router
    a = handle.start_attempt({"attempt_index": 1, "channel_id": "channel-secret-42"})
    a.start()
    handle.finish_attempt(a, upstream_status=200,
                         raw_usage={"prompt_tokens": 5, "completion_tokens": 5})
    a.close()
    handle.finalize()

    text = str(router.span.to_record()) + str(a.span.to_record())
    for secret in ("channel-secret-42", "sk-", "Authorization", "Bearer "):
        assert secret not in text, f"secret leaked: {secret}"
    assert "gateway.channel_id" in text  # hashed value present
