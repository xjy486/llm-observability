"""Fallback semantics tests (spec §13.3, §10)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import GatewayRuntime, PrivacyGuard
from llm_observability.gateway_observability.events import EVENT_FALLBACK_SELECTED


def _handle():
    return GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s")).handle_request(
        {"gateway_name": "mock"}
    )


def _event(span, name):
    return [e for e in span.events if e["name"] == name]


def test_fallback_records_single_event_with_from_to(clean_sdk):
    handle = _handle()
    router = handle.router

    # Attempt 1 on channel-a times out
    a1 = handle.start_attempt({"attempt_index": 1, "channel_id": "ch-a"})
    a1.start()
    handle.finish_attempt(a1, error=TimeoutError("timed out"))
    a1.close()

    # Fallback from ch-a to ch-b
    assert router.fallback_selected(
        from_channel_id="ch-a", to_channel_id="ch-b", reason="timeout"
    ) is True

    # Fresh attempt on ch-b
    a2 = handle.start_attempt({"attempt_index": 2, "channel_id": "ch-b"})
    a2.start()
    handle.finish_attempt(a2, upstream_status=200)
    a2.close()

    handle.finalize()

    events = _event(router.span, EVENT_FALLBACK_SELECTED)
    assert len(events) == 1
    attrs = events[0]["attributes"]
    # From/to channels differ; both are recorded — hashed (rework P0-8).
    assert router.fallback_count == 1
    assert attrs["from_channel_id"] == router._privacy.hash_channel_id("ch-a")
    assert attrs["to_channel_id"] == router._privacy.hash_channel_id("ch-b")
    assert "ch-a" not in str(attrs.values())
    assert "ch-b" not in str(attrs.values())
    assert attrs["reason"] == "timeout"
    assert router.final_channel_id == "ch-b"
    assert router.span.attributes["gateway.fallback_count"] == 1


def test_fallback_from_equals_to_is_rejected(clean_sdk):
    handle = _handle()
    router = handle.router
    # Fallback without a channel switch is forbidden.
    assert router.fallback_selected(
        from_channel_id="ch-a", to_channel_id="ch-a", reason="timeout"
    ) is False
    assert router.fallback_count == 0
    assert len(_event(router.span, EVENT_FALLBACK_SELECTED)) == 0
    handle.finalize()


def test_fallback_router_final_channel_is_last(clean_sdk):
    handle = _handle()
    router = handle.router
    a1 = handle.start_attempt({"attempt_index": 1, "channel_id": "ch-a"})
    a1.start()
    handle.finish_attempt(a1, error=TimeoutError("timeout"))
    a1.close()
    router.fallback_selected(from_channel_id="ch-a", to_channel_id="ch-c", reason="timeout")
    a2 = handle.start_attempt({"attempt_index": 2, "channel_id": "ch-c"})
    a2.start()
    handle.finish_attempt(a2, upstream_status=200)
    a2.close()
    handle.finalize()
    assert router.span.status == "OK"
    # Router channel recorded hashed (privacy), internal final channel is ch-c
    assert router.final_channel_id == "ch-c"
    assert router.span.attributes["gateway.channel_id"] != "ch-c"
    assert len(router.span.attributes["gateway.channel_id"]) == 16
    assert router.success_count == 1
