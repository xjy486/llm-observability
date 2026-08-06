"""Blocker (final): streaming terminal states PUBLISH the result to the Router
BEFORE closing the Attempt — so a Router.finalize() racing the gap reports a
Router Record that already includes the result.

The tests patch ``AttemptSpan.try_aggregate_result`` to block inside the publish
window (the Attempt is still registered), run the stream to a terminal state,
release a Router.finalize() on another thread, then release the publish — and
assert the REPORTED Router Record (status, final_error_category, usage, cost,
attempt_count), not just the in-memory counts.
"""
import os
import sys
import threading
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
from llm_observability.gateway_observability.attributes import ATTR_ROUTER
from llm_observability.gateway_observability.context import clear_gateway_context


_PRICING = {
    "gpt-5.6": {"input_usd_per_1m_tokens": 2.0, "output_usd_per_1m_tokens": 8.0},
}


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


def _make_handle(tracer, chunks, resolved_model="gpt-5.6"):
    runtime = _runtime(tracer)
    handle = runtime.handle_request({"gateway_name": "mock", "requested_model": resolved_model})
    attempt = handle.start_attempt({"resolved_model": resolved_model, "channel_id": "ch-mock"})
    attempt.start()
    return handle, handle.router, attempt, chunks


class _ReportSpy:
    """Synchronous recorder replacing the async reporter."""

    def __init__(self, tracer):
        self.records = []
        self._original = tracer.reporter.report
        tracer.reporter.report = self._capture

    def _capture(self, record):
        self.records.append(record)

    def restore(self, tracer):
        tracer.reporter.report = self._original

    def router_record(self, router):
        for r in self.records:
            if isinstance(r, dict) and r.get("span_id") == router.span.span_id:
                return r
        return None


def _run_stream_with_blocked_publish(tracer, chunks, terminal_path, build_result_check):
    """Shared harness: patch try_aggregate_result to block inside the publish
    window; run the stream's terminal path on a worker thread (blocked at
    publish); finalize the Router on another thread; release; assert on the
    reported Router Record via ``build_result_check(spy, router)``.

    ``terminal_path(stream)`` drives the stream to its terminal state
    (e.g. ``list(stream)`` for success, ``with pytest.raises: list(stream)``
    for error)."""
    handle, router, attempt, _ = _make_handle(tracer, chunks)
    spy = _ReportSpy(tracer)
    publish_entered = threading.Event()
    publish_release = threading.Event()
    real_try_agg = attempt.try_aggregate_result

    def blocked_try_agg(result):
        # Inside _publish_and_close, BEFORE _close_attempt — the Attempt is
        # still registered. Block here so a Router.finalize() races the gap.
        publish_entered.set()
        publish_release.wait(timeout=5)
        return real_try_agg(result)

    attempt.try_aggregate_result = blocked_try_agg
    finalize_done = threading.Event()

    def stream_runner():
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        terminal_path(stream)

    def finalizer():
        handle.finalize()
        finalize_done.set()

    try:
        ts = threading.Thread(target=stream_runner)
        ts.start()
        assert publish_entered.wait(timeout=5), "did not reach the publish window"
        tf = threading.Thread(target=finalizer)
        tf.start()
        time.sleep(0.05)  # let finalize block on the still-open Attempt
        publish_release.set()  # publish proceeds (Attempt stays registered during publish)
        ts.join(timeout=5)
        tf.join(timeout=5)
        build_result_check(spy, router, attempt)
    finally:
        spy.restore(tracer)


class TestStreamPublishBeforeClose:
    def test_stream_success_publishes_before_attempt_unregister(self, tracer):
        chunks = iter([
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ])

        def check(spy, router, attempt):
            rec = spy.router_record(router)
            assert rec is not None, "Router must be reported"
            assert rec["attributes"].get("usage.total_tokens") == 15
            assert rec["attributes"].get(ATTR_ROUTER["attempt_count"]) == 1

        _run_stream_with_blocked_publish(tracer, chunks, lambda s: list(s), check)

    def test_stream_error_publishes_before_router_report(self, tracer):
        def exploding():
            yield {"choices": [{"delta": {"content": "a"}}]}
            raise TimeoutError("upstream reset")

        def terminal(stream):
            with pytest.raises(TimeoutError):
                list(stream)

        def check(spy, router, attempt):
            rec = spy.router_record(router)
            assert rec is not None
            assert rec["status"] == "ERROR"
            assert rec["attributes"].get(ATTR_ROUTER["final_error_category"]) == ErrorCategory.TIMEOUT

        _run_stream_with_blocked_publish(tracer, exploding(), terminal, check)

    def test_stream_cancel_publishes_before_router_report(self, tracer):
        chunks = iter([{"choices": [{"delta": {"content": "a"}}]}])

        def terminal(stream):
            it = iter(stream)
            next(it)
            stream.close()  # client cancel

        def check(spy, router, attempt):
            rec = spy.router_record(router)
            assert rec is not None
            assert rec["status"] == "ERROR"
            assert rec["attributes"].get(ATTR_ROUTER["final_error_category"]) == ErrorCategory.CLIENT_CANCELLED

        _run_stream_with_blocked_publish(tracer, chunks, terminal, check)

    def test_stream_usage_cost_present_in_report_under_finalize_race(self, tracer):
        chunks = iter([
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ])

        def check(spy, router, attempt):
            rec = spy.router_record(router)
            assert rec is not None
            assert rec["attributes"].get("usage.total_tokens") == 15
            assert rec["attributes"].get("cost.source") in ("priced", "unpriced")

        _run_stream_with_blocked_publish(tracer, chunks, lambda s: list(s), check)

    def test_stream_reported_router_matches_final_memory_state(self, tracer):
        chunks = iter([
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ])

        def check(spy, router, attempt):
            rec = spy.router_record(router)
            assert rec is not None
            # Reported Record matches the final in-memory state.
            assert rec["attributes"].get("usage.total_tokens") == router.usage_aggregate.total_tokens
            assert rec["attributes"].get(ATTR_ROUTER["attempt_count"]) == router.attempt_count
            assert (rec["status"] == "OK") == (router.final_error is None)

        _run_stream_with_blocked_publish(tracer, chunks, lambda s: list(s), check)
