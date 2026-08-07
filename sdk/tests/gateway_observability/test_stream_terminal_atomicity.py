"""P0-1: Streaming Terminal atomic state machine (first terminal claim wins).

The three streaming terminal paths (``finalize_success`` / ``finalize_error`` /
``finalize_cancelled``) race for a single Terminal State via ``_claim_terminal``.
Exactly one path wins; every losing path is a no-op that writes no Attempt
error/status, records no terminal event, aggregates nothing, and closes
nothing. No ``sleep()`` timing — races are made deterministic with
``threading.Barrier`` / ``threading.Event`` / ``asyncio.Event``.
"""
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    ErrorCategory,
    GatewayStream,
    CostCalculator,
)
from llm_observability.gateway_observability.attributes import ATTR_ATTEMPT, ATTR_ROUTER
from llm_observability.gateway_observability.context import clear_gateway_context
from llm_observability.gateway_observability.events import (
    EVENT_STREAM_COMPLETED,
    EVENT_STREAM_CANCELLED,
    EVENT_ATTEMPT_COMPLETED,
    EVENT_ATTEMPT_FAILED,
    EVENT_RESPONSE_COMPLETED,
    EVENT_RESPONSE_FAILED,
)


_PRICING = {
    "gpt-5.6": {"input_usd_per_1m_tokens": 2.0, "output_usd_per_1m_tokens": 8.0},
}


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


def _make_handle(tracer, resolved_model="gpt-5.6"):
    runtime = GatewayRuntime(
        tracer=tracer, sample_rate=1.0, privacy=PrivacyGuard(secret="s"),
        cost_calculator=CostCalculator(pricing_table=_PRICING),
    )
    handle = runtime.handle_request({"gateway_name": "mock", "requested_model": resolved_model})
    attempt = handle.start_attempt({"resolved_model": resolved_model, "channel_id": "ch-mock"})
    attempt.start()
    return handle, handle.router, attempt


def _make_stream(tracer, chunks=None):
    handle, router, attempt = _make_handle(tracer)
    if chunks is None:
        chunks = iter([{"choices": [{"delta": {"content": "hi"}}]}])
    stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
    return handle, router, attempt, stream


def _events(span, name):
    return [e for e in span.events if e["name"] == name]


def _race_two_finalizers(tracer, path_a, path_b):
    """Build one stream and race two terminal finalizer methods on it behind a
    Barrier(2) so both reach ``_claim_terminal`` concurrently.

    ``path_a`` / ``path_b`` are callables that receive the finalizer and drive
    it to a terminal state (e.g. ``lambda f: f.finalize_success()``). Returns
    (handle, router, attempt, stream, finalizer) after both threads join."""
    handle, router, attempt, stream = _make_stream(tracer)
    finalizer = stream._finalizer
    barrier = threading.Barrier(2)
    errors = []

    def run(fn):
        try:
            barrier.wait()
        except Exception as e:  # BrokenBarrierError etc.
            errors.append(("barrier", repr(e)))
            return
        try:
            fn(finalizer)
        except Exception as e:
            errors.append(("fn", type(e).__name__, repr(e)))

    t1 = threading.Thread(target=run, args=(path_a,))
    t2 = threading.Thread(target=run, args=(path_b,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not errors, f"thread errors: {errors}"
    assert not t1.is_alive() and not t2.is_alive(), "thread still alive"
    return handle, router, attempt, stream, finalizer


class TestStreamTerminalAtomicClaim:
    def test_stream_exhaustion_racing_close_claims_one_terminal(self, tracer):
        handle, router, attempt, stream, finalizer = _race_two_finalizers(
            tracer,
            lambda f: f.finalize_success(),
            lambda f: f.finalize_cancelled(),
        )
        # Exactly one terminal state claimed.
        assert finalizer.terminal_state in ("success", "cancelled")
        # Exactly one aggregation.
        assert router.success_count + router.fail_count == 1
        # Exactly one stream terminal event.
        n = len(_events(router.span, EVENT_STREAM_COMPLETED)) + \
            len(_events(router.span, EVENT_STREAM_CANCELLED))
        assert n == 1
        # Router and Attempt terminal statuses agree.
        assert (router.span.status == "OK") == (attempt.span.status == "OK")

    def test_stream_error_racing_close_claims_one_terminal(self, tracer):
        handle, router, attempt, stream, finalizer = _race_two_finalizers(
            tracer,
            lambda f: f.finalize_error(TimeoutError("upstream reset")),
            lambda f: f.finalize_cancelled(),
        )
        assert finalizer.terminal_state in ("error", "cancelled")
        assert router.success_count + router.fail_count == 1
        n = len(_events(router.span, EVENT_STREAM_COMPLETED)) + \
            len(_events(router.span, EVENT_STREAM_CANCELLED))
        assert n == 1
        # Both ERROR regardless of which won (error → ERROR; cancel → ERROR).
        assert router.span.status == "ERROR"
        assert attempt.span.status == "ERROR"

    def test_done_marker_racing_disconnect_claims_one_terminal(self, tracer):
        # done-marker → finalize_success; disconnect → finalize_cancelled.
        handle, router, attempt, stream, finalizer = _race_two_finalizers(
            tracer,
            lambda f: f.finalize_success(),
            lambda f: f.finalize_cancelled(),
        )
        assert finalizer.terminal_state in ("success", "cancelled")
        assert router.success_count + router.fail_count == 1
        n = len(_events(router.span, EVENT_STREAM_COMPLETED)) + \
            len(_events(router.span, EVENT_STREAM_CANCELLED))
        assert n == 1

    def test_async_cancel_racing_aclose_claims_one_terminal(self, tracer):
        # Both paths are cancel; first claim wins, the second is a no-op.
        handle, router, attempt, stream, finalizer = _race_two_finalizers(
            tracer,
            lambda f: f.finalize_cancelled(),
            lambda f: f.finalize_cancelled(),
        )
        assert finalizer.terminal_state == "cancelled"
        assert router.fail_count == 1
        assert router.success_count == 0
        assert len(_events(router.span, EVENT_STREAM_CANCELLED)) == 1


class TestStreamTerminalRecordsOneEvent:
    def test_stream_terminal_records_one_stream_event(self, tracer):
        handle, router, attempt, stream = _make_stream(tracer)
        list(stream)  # success
        assert len(_events(router.span, EVENT_STREAM_COMPLETED)) == 1
        assert len(_events(router.span, EVENT_STREAM_CANCELLED)) == 0

    def test_stream_terminal_records_one_attempt_event(self, tracer):
        handle, router, attempt, stream = _make_stream(tracer)
        list(stream)
        # Exactly one of attempt.completed/failed on the Attempt span.
        n = len(_events(attempt.span, EVENT_ATTEMPT_COMPLETED)) + \
            len(_events(attempt.span, EVENT_ATTEMPT_FAILED))
        assert n == 1

    def test_stream_terminal_records_one_router_event(self, tracer):
        handle, router, attempt, stream = _make_stream(tracer)
        list(stream)
        n = len(_events(router.span, EVENT_RESPONSE_COMPLETED)) + \
            len(_events(router.span, EVENT_RESPONSE_FAILED))
        assert n == 1

    def test_stream_cancel_records_one_stream_event(self, tracer):
        handle, router, attempt, stream = _make_stream(tracer)
        stream.close()
        assert len(_events(router.span, EVENT_STREAM_CANCELLED)) == 1
        assert len(_events(router.span, EVENT_STREAM_COMPLETED)) == 0


class TestStreamTerminalConsistency:
    def test_stream_terminal_router_attempt_status_consistent(self, tracer):
        # Success → both OK.
        h1, r1, a1, s1 = _make_stream(tracer)
        list(s1)
        assert r1.span.status == "OK" and a1.span.status == "OK"
        # Cancel → both ERROR.
        h2, r2, a2, s2 = _make_stream(tracer)
        s2.close()
        assert r2.span.status == "ERROR" and a2.span.status == "ERROR"
        assert a2.span.attributes[ATTR_ATTEMPT["error_category"]] == ErrorCategory.CLIENT_CANCELLED
        # Error → both ERROR.
        h3, r3, a3, s3 = _make_stream(tracer, chunks=_exploding_iter())
        with pytest.raises(TimeoutError):
            list(s3)
        assert r3.span.status == "ERROR" and a3.span.status == "ERROR"

    def test_stream_terminal_aggregates_exactly_once(self, tracer):
        handle, router, attempt, stream = _make_stream(tracer)
        list(stream)
        assert router.success_count + router.fail_count == 1
        # Idempotent re-close does not re-aggregate.
        stream.close()
        assert router.success_count + router.fail_count == 1


def _exploding_iter():
    def gen():
        yield {"choices": [{"delta": {"content": "a"}}]}
        raise TimeoutError("upstream reset")
    return gen()
