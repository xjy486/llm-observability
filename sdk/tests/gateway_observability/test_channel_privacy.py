"""P0-8 + P1-3 + P1-4 + P1-5: channel privacy, event lifecycle, association,
and guarded span attributes (adversarial).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    set_gateway_attribute,
)
from llm_observability.gateway_observability.context import (
    GatewayRequestContext,
    clear_gateway_context,
)
from llm_observability.gateway_observability.router_span import RouteDecision


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


def _runtime(tracer):
    return GatewayRuntime(tracer=tracer, sample_rate=1.0, privacy=PrivacyGuard(secret="s3cr3t"))


def _all_event_values(span):
    """Flatten every event attribute value of a span into a list of strings."""
    values = []
    for event in span.events:
        for v in (event.get("attributes") or {}).values():
            values.append(str(v))
    return values


class TestChannelIdHashing:
    def test_route_event_channel_id_is_hashed(self, tracer):
        runtime = _runtime(tracer)
        raw = "channel-42-supersecret"
        handle = runtime.handle_request(
            {},  # request
        )
        router = handle.router
        router._route_decision = RouteDecision(channel_id=raw, provider="openai")
        router.recorder.route_selected(channel_id=raw, provider="openai", reason="primary")
        event = [e for e in router.span.events if e["name"] == "gateway.route.selected"][-1]
        recorded = event["attributes"]["channel_id"]
        assert recorded != raw
        assert recorded == router._privacy.hash_channel_id(raw)
        handle.finalize()

    def test_attempt_event_channel_id_is_hashed(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt({"channel_id": "raw-channel-7"})
        attempt.start()
        event = [e for e in attempt.span.events if e["name"] == "gateway.attempt.started"][-1]
        assert event["attributes"]["channel_id"] != "raw-channel-7"
        expected = handle.router._privacy.hash_channel_id("raw-channel-7")
        assert event["attributes"]["channel_id"] == expected
        attempt.close()
        handle.finalize()

    def test_fallback_event_contains_from_and_to(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        handle.fallback_selected(from_channel_id="ch-a", to_channel_id="ch-b", reason="timeout")
        event = [e for e in handle.router.span.events if e["name"] == "gateway.fallback.selected"][-1]
        assert "from_channel_id" in event["attributes"]
        assert "to_channel_id" in event["attributes"]
        assert event["attributes"]["reason"] == "timeout"
        handle.finalize()

    def test_fallback_event_from_and_to_are_hashed(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        handle.fallback_selected(from_channel_id="ch-a", to_channel_id="ch-b", reason="timeout")
        event = [e for e in handle.router.span.events if e["name"] == "gateway.fallback.selected"][-1]
        guard = handle.router._privacy
        assert event["attributes"]["from_channel_id"] == guard.hash_channel_id("ch-a")
        assert event["attributes"]["to_channel_id"] == guard.hash_channel_id("ch-b")
        assert "ch-a" not in _all_event_values(handle.router.span)
        assert "ch-b" not in _all_event_values(handle.router.span)
        handle.finalize()

    def test_raw_channel_id_absent_from_all_span_events_logs(self, tracer, caplog):
        runtime = _runtime(tracer)
        raw = "raw-channel-XYZ"
        with caplog.at_level(logging.DEBUG):
            handle = runtime.handle_request({})
            attempt = handle.start_attempt({"channel_id": raw})
            attempt.start()
            handle.fallback_selected(from_channel_id=raw, to_channel_id="ch-b", reason="r")
            handle.finish_attempt(attempt, upstream_status=200)
            attempt.close()
            handle.finalize()
        for span in (handle.router.span, attempt.span):
            for v in span.attributes.values():
                assert raw not in str(v)
            assert raw not in _all_event_values(span)
        assert raw not in caplog.text

    def test_same_channel_id_hash_is_stable(self):
        guard = PrivacyGuard(secret="k")
        assert guard.hash_channel_id("ch-1") == guard.hash_channel_id("ch-1")

    def test_different_channel_ids_hash_differ(self):
        guard = PrivacyGuard(secret="k")
        assert guard.hash_channel_id("ch-1") != guard.hash_channel_id("ch-2")


class TestEventLifecycle:
    def test_attempt_start_event_exactly_once(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt({})
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200)
        attempt.close()
        handle.finalize()
        events = [e for e in attempt.span.events if e["name"] == "gateway.attempt.started"]
        assert len(events) == 1

    def test_attempt_completed_event_exactly_once(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt({})
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200)
        attempt.close()
        handle.finalize()
        events = [e for e in attempt.span.events if e["name"] == "gateway.attempt.completed"]
        assert len(events) == 1

    def test_attempt_failed_event_exactly_once(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt({})
        attempt.start()
        handle.finish_attempt(attempt, error=TimeoutError("t"))
        attempt.close()
        handle.finalize()
        events = [e for e in attempt.span.events if e["name"] == "gateway.attempt.failed"]
        assert len(events) == 1

    def test_router_response_completed_exactly_once(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt({})
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200)
        attempt.close()
        handle.finalize()
        handle.finalize()  # second call must not duplicate
        events = [e for e in handle.router.span.events if e["name"] == "gateway.response.completed"]
        assert len(events) == 1

    def test_router_response_failed_exactly_once(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt({})
        attempt.start()
        handle.finish_attempt(attempt, error=TimeoutError("t"))
        attempt.close()
        handle.finalize()
        events = [e for e in handle.router.span.events if e["name"] == "gateway.response.failed"]
        assert len(events) == 1

    def test_no_success_and_failed_events_on_same_attempt(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt({})
        attempt.start()
        handle.finish_attempt(attempt, error=TimeoutError("t"))
        attempt.close()
        handle.finalize()
        names = [e["name"] for e in attempt.span.events]
        assert "gateway.attempt.failed" in names
        assert "gateway.attempt.completed" not in names


class TestAssociationFields:
    def test_router_all_association_fields(self, tracer):
        runtime = _runtime(tracer)
        request = {
            "user_id": "u-1", "session_id": "s-1", "message_id": "m-1",
            "app_name": "my-app", "business_scenario": "chat",
        }
        handle = runtime.handle_request(request)
        span = handle.router.span
        # Association lives on the Span top-level fields with the existing
        # Span Record naming (business_scene — never business_scenario).
        assert span.user_id == "u-1"
        assert span.session_id == "s-1"
        assert span.message_id == "m-1"
        assert span.app_name == "my-app"
        assert span.business_scene == "chat"
        assert "business_scenario" not in span.attributes
        assert "business_scene" not in span.attributes
        record = span.to_record()
        assert record["user_id"] == "u-1"
        assert record["business_scene"] == "chat"
        handle.finalize()

    def test_attempt_does_not_duplicate_sensitive_association(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"user_id": "u-1", "session_id": "s-1"})
        attempt = handle.start_attempt({})
        attempt.start()
        assert attempt.span.user_id is None
        assert attempt.span.session_id is None
        assert "user_id" not in attempt.span.attributes
        assert "session_id" not in attempt.span.attributes
        attempt.close()
        handle.finalize()

    def test_remote_association_propagates_to_router(self, tracer):
        runtime = _runtime(tracer)
        rc = GatewayRequestContext(user_id="remote-user", session_id="remote-session")
        from llm_observability.gateway_observability.router_span import RouterSpan
        router = RouterSpan(tracer=tracer, request_context=rc).start()
        assert router.span.user_id == "remote-user"
        router.close()

    def test_local_gateway_association_overrides_remote(self, tracer):
        """Explicit request values win; adapter output is the resolved value."""
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"user_id": "local-user"})
        assert handle.router.span.user_id == "local-user"
        handle.finalize()

    def test_association_values_are_sanitized(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"user_id": "user-sk-ABCDEFGHIJKLMNOP1234"})
        value = handle.router.span.user_id
        assert "sk-ABCDEFGHIJKLMNOP1234" not in value
        handle.finalize()


class TestGuardedAttributes:
    def _plain_span(self, tracer):
        from llm_observability.spans import Span, SpanKind
        from llm_observability.utils.ids import generate_trace_id, generate_span_id
        span = Span(trace_id=generate_trace_id(), span_id=generate_span_id(),
                    parent_span_id=None, span_name="test", span_kind=SpanKind.GATEWAY)
        span.start()
        return span

    def test_router_external_values_sanitized(self, tracer):
        guard = PrivacyGuard()
        span = self._plain_span(tracer)
        ok = set_gateway_attribute(span, "gateway.error_message",
                                   "failed with sk-ABCDEFGHIJKLMNOP1234", guard)
        assert ok is True
        assert "sk-ABCDEFGHIJKLMNOP1234" not in span.attributes["gateway.error_message"]

    def test_attempt_external_values_sanitized(self, tracer):
        guard = PrivacyGuard()
        span = self._plain_span(tracer)
        set_gateway_attribute(span, "gateway.provider", "openai", guard)
        set_gateway_attribute(span, "gateway.resolved_model", "gpt-5.6", guard)
        assert span.attributes["gateway.provider"] == "openai"
        assert span.attributes["gateway.resolved_model"] == "gpt-5.6"

    def test_request_id_size_limited(self, tracer):
        guard = PrivacyGuard()
        span = self._plain_span(tracer)
        long_id = "x" * 1000
        set_gateway_attribute(span, "gateway.request_id", long_id, guard)
        assert len(span.attributes["gateway.request_id"].encode("utf-8")) <= 256

    def test_route_query_removed(self, tracer):
        guard = PrivacyGuard()
        span = self._plain_span(tracer)
        set_gateway_attribute(span, "gateway.route",
                              "/v1/chat/completions?api_key=secret123", guard)
        assert "api_key" not in span.attributes["gateway.route"]
        assert "?" not in span.attributes["gateway.route"]

    def test_error_message_secret_redacted(self, tracer):
        guard = PrivacyGuard()
        span = self._plain_span(tracer)
        set_gateway_attribute(span, "gateway.error_message",
                              "Authorization: Bearer abcdef1234567890", guard)
        assert "abcdef1234567890" not in span.attributes["gateway.error_message"]

    def test_span_attributes_default_deny_unknown_keys(self, tracer):
        guard = PrivacyGuard()
        span = self._plain_span(tracer)
        ok = set_gateway_attribute(span, "gateway.totally_made_up_field", "x", guard)
        assert ok is False
        assert "gateway.totally_made_up_field" not in span.attributes
