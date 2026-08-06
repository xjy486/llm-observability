"""P1-1: Streaming Duration field semantics.

``gateway.upstream_duration_ms`` on a streaming Attempt SHALL reflect the full
upstream stream lifecycle (``terminal_time - attempt_start_time``), NOT the
response-header time recorded at wrapper creation. ``upstream_connect_duration_ms``
is unchanged. Non-streaming Attempt duration semantics are unchanged.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    ErrorCategory,
    GatewayStream,
    CostCalculator,
)
from llm_observability.gateway_observability.attributes import ATTR_ATTEMPT
from llm_observability.gateway_observability.context import clear_gateway_context


_PRICING = {
    "gpt-5.6": {"input_usd_per_1m_tokens": 2.0, "output_usd_per_1m_tokens": 8.0},
}
_HEADER_DURATION_MS = 300.0  # response-header time, distinct from total lifecycle


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


def _runtime(tracer):
    return GatewayRuntime(
        tracer=tracer, sample_rate=1.0, privacy=PrivacyGuard(secret="s"),
        cost_calculator=CostCalculator(pricing_table=_PRICING),
    )


def _make_stream(tracer, chunks, header_duration_ms=_HEADER_DURATION_MS,
                 connect_duration_ms=20.0):
    runtime = _runtime(tracer)
    handle = runtime.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    attempt = handle.start_attempt({"resolved_model": "gpt-5.6", "channel_id": "ch-mock"})
    attempt.start()
    stream = GatewayStream(
        chunks, handle.router, attempt, runtime_handle=handle,
        upstream_status=200, duration_ms=header_duration_ms,
        connect_duration_ms=connect_duration_ms,
    )
    return handle, handle.router, attempt, stream


def _duration_attr(attempt):
    return attempt.span.attributes.get(ATTR_ATTEMPT["upstream_duration_ms"])


def _connect_attr(attempt):
    return attempt.span.attributes.get(ATTR_ATTEMPT["upstream_connect_duration_ms"])


class TestStreamDurationSemantics:
    def test_stream_headers_duration_distinct_from_total_duration(self, tracer):
        # The header-time duration (300ms) is recorded at wrapper creation but
        # MUST be overwritten at the terminal state by the full lifecycle.
        handle, router, attempt, stream = _make_stream(
            tracer, iter([{"choices": [{"delta": {"content": "hi"}}]}]),
        )
        # Before terminal: the header duration is on the span.
        assert _duration_attr(attempt) == _HEADER_DURATION_MS
        list(stream)  # drive to success terminal
        # After terminal: overwritten with the full lifecycle, NOT the header time.
        final = _duration_attr(attempt)
        assert final is not None
        assert final != _HEADER_DURATION_MS
        # Connect duration untouched.
        assert _connect_attr(attempt) == 20.0

    def test_stream_total_duration_covers_consumption(self, tracer):
        def chunks():
            yield {"choices": [{"delta": {"content": "a"}}]}
            time.sleep(0.08)  # measurable consumption window
            yield {"choices": [{"delta": {"content": "b"}}]}

        handle, router, attempt, stream = _make_stream(tracer, chunks())
        list(stream)
        final = _duration_attr(attempt)
        assert final is not None
        # Full lifecycle covers the consumption window (>= ~40ms of the 80ms sleep).
        assert final >= 40.0
        # And it is not the header-time value.
        assert final != _HEADER_DURATION_MS

    def test_stream_cancel_total_duration_covers_partial_consumption(self, tracer):
        def chunks():
            yield {"choices": [{"delta": {"content": "a"}}]}
            time.sleep(0.08)
            yield {"choices": [{"delta": {"content": "b"}}]}

        handle, router, attempt, stream = _make_stream(tracer, chunks())
        it = iter(stream)
        next(it)  # consume one chunk (partial consumption)
        time.sleep(0.05)
        stream.close()  # client cancel terminal
        final = _duration_attr(attempt)
        assert final is not None
        # Partial-consumption window up to the cancel is covered.
        assert final >= 40.0
        assert final != _HEADER_DURATION_MS
        # Cancel semantics preserved.
        assert attempt.span.attributes[ATTR_ATTEMPT["error_category"]] == ErrorCategory.CLIENT_CANCELLED

    def test_non_streaming_duration_semantics_unchanged(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
        attempt = handle.start_attempt({"resolved_model": "gpt-5.6", "channel_id": "ch-mock"})
        attempt.start()
        # Non-streaming finalize carries an explicit duration; the streaming
        # overwrite path MUST NOT touch it.
        handle.finish_attempt(attempt, upstream_status=200, duration_ms=42.0)
        attempt.close()
        handle.finalize()
        assert _duration_attr(attempt) == 42.0
