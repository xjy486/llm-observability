"""Retry semantics tests (spec §13.2, §4.2)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import GatewayRuntime, PrivacyGuard
from llm_observability.gateway_observability.events import EVENT_RETRY_SCHEDULED


def _handle():
    return GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s")).handle_request(
        {"gateway_name": "mock"}
    )


def _event(span, name):
    return [e for e in span.events if e["name"] == name]


def test_retry_scheduled_records_event_and_fresh_attempt(clean_sdk):
    handle = _handle()
    router = handle.router

    a1 = handle.start_attempt({"attempt_index": 1, "channel_id": "ch-a"})
    a1.start()
    handle.finish_attempt(a1, upstream_status=500)
    a1.close()

    # Retry decision recorded on the Router
    assert router.retry_scheduled(attempt_index=1, delay_ms=200.0, reason="provider_5xx") is True

    # Fresh attempt 2
    a2 = handle.start_attempt({"attempt_index": 2, "channel_id": "ch-a"})
    a2.start()
    handle.finish_attempt(a2, upstream_status=200)
    a2.close()

    handle.finalize()

    assert router.span.attributes["gateway.retry_count"] == 1
    assert router.span.attributes["gateway.attempt_count"] == 2

    retry_events = _event(router.span, EVENT_RETRY_SCHEDULED)
    assert len(retry_events) == 1
    attrs = retry_events[0]["attributes"]
    assert attrs["attempt_index"] == 1
    assert attrs["delay_ms"] == 200.0
    assert attrs["reason"] == "provider_5xx"

    # Two distinct attempt spans, same trace
    attempts = [e for e in router.span.events]
    assert len(router.attempts) == 2
    assert router.attempts[0].span.span_id != router.attempts[1].span.span_id
    assert router.attempts[0].span.trace_id == router.attempts[1].span.trace_id
    assert router.attempts[1].span.attributes["gateway.attempt_index"] == 2


def test_retry_semantics_router_aggregate_includes_failed_attempt(clean_sdk):
    """Retry cost preserved: failed attempt usage counts in Router aggregate."""
    handle = _handle()
    router = handle.router

    a1 = handle.start_attempt({"attempt_index": 1})
    a1.start()
    # Failed attempt with billable upstream usage
    handle.finish_attempt(a1, upstream_status=500,
                         raw_usage={"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100})
    a1.close()

    a2 = handle.start_attempt({"attempt_index": 2})
    a2.start()
    handle.finish_attempt(a2, upstream_status=200,
                         raw_usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
    a2.close()

    handle.finalize()

    # Router status OK (final attempt succeeded) but usage includes failed attempt
    assert router.span.status == "OK"
    assert router.span.attributes["usage.input_tokens"] == 200
    assert router.span.attributes["usage.output_tokens"] == 50
    assert router.span.attributes["usage.total_tokens"] == 250
    assert router.fail_count == 1
    assert router.success_count == 1
