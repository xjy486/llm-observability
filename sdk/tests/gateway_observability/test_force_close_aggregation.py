"""Blocker 1: force_close() must aggregate the failure to the Router.

A Router that finalizes with an open Attempt must end ERROR (never OK while
its child Attempt is ERROR): fail_count increments, final_error_category is
gateway_internal (unless a business error supersedes), and exactly one
gateway.response.failed is recorded. Aggregation is exactly-once and never
re-aggregates an already-finalized attempt.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability.attributes import ATTR_ATTEMPT, ATTR_ROUTER
from llm_observability.gateway_observability.context import clear_gateway_context
from llm_observability.gateway_observability.errors import ErrorCategory, GatewayError
from llm_observability.gateway_observability.runtime import GatewayRuntime


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


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


def _router_events(router, name):
    return [e for e in router.span.events if e["name"] == name]


class TestForceCloseAggregation:
    def test_force_closed_attempt_makes_router_error(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        handle.finalize()

        assert handle.router.span.status == "ERROR", (
            "Router must be ERROR when it force-closed an open Attempt"
        )

    def test_force_closed_attempt_sets_router_final_error(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        handle.finalize()

        assert handle.router.fail_count == 1
        assert handle.router.span.attributes[ATTR_ROUTER["final_error_category"]] == (
            ErrorCategory.GATEWAY_INTERNAL
        )
        final_error = handle.router.final_error
        assert final_error is not None
        assert final_error.category == ErrorCategory.GATEWAY_INTERNAL

    def test_force_closed_attempt_records_response_failed(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        handle.finalize()

        failed = _router_events(handle.router, "gateway.response.failed")
        completed = _router_events(handle.router, "gateway.response.completed")
        assert len(failed) == 1, "exactly one response_failed"
        assert len(completed) == 0, "no response_completed when force-closed"

    def test_multiple_force_closed_attempts_increment_fail_count(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        for _ in range(3):
            a = handle.start_attempt()
            a.start()
        handle.finalize()

        assert handle.router.fail_count == 3
        assert handle.router.success_count == 0
        assert handle.router.span.status == "ERROR"
        # Each attempt reported exactly once.
        spy_records = list(tracer.reporter._queue)
        attempt_span_ids = {a.span.span_id for a in handle.router.attempts}
        for sid in attempt_span_ids:
            reports = [r for r in spy_records if isinstance(r, dict) and r.get("span_id") == sid]
            assert len(reports) == 1, f"attempt {sid} reported {len(reports)} times"

    def test_force_closed_attempt_with_business_error_preserves_it(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        attempt.set_error(GatewayError(
            category=ErrorCategory.TIMEOUT, type="TimeoutError",
            message="upstream timed out", retryable=True,
        ))
        handle.finalize()  # force-closes; business error must win over gateway_internal

        assert handle.router.span.attributes[ATTR_ROUTER["final_error_category"]] == (
            ErrorCategory.TIMEOUT
        )
        assert handle.router.final_error.category == ErrorCategory.TIMEOUT

    def test_force_closed_attempt_does_not_duplicate_report(self, tracer):
        spy = _ReportSpy(tracer)
        try:
            runtime = GatewayRuntime(tracer=tracer)
            handle = runtime.handle_request({})
            attempt = handle.start_attempt()
            attempt.start()
            handle.finalize()
            attempt_span_id = attempt.span.span_id
            before = len(spy.records)
            # Re-finalize the router and re-force-close: must not re-aggregate
            # or re-report.
            handle.finalize()
            attempt.force_close()
            assert len(spy.records) == before
            attempt_reports = [
                r for r in spy.records
                if isinstance(r, dict) and r.get("span_id") == attempt_span_id
            ]
            assert len(attempt_reports) == 1
        finally:
            spy.restore(tracer)


class TestForceCloseFinalizedButOpen:
    """Blocker 1 (follow-up): an attempt that was finalized (result aggregated)
    but never ``close()``d must NOT be flipped to gateway_internal at Router
    finalize — the already-aggregated outcome (OK or business error) stands.
    """

    def test_finalized_success_but_open_force_close_keeps_both_ok(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        # Outcome aggregated as success, but close() forgotten.
        handle.finish_attempt(attempt, upstream_status=200, duration_ms=12.0)
        assert attempt._aggregated_to_router is True
        assert handle.router.success_count == 1

        handle.finalize()  # force-closes the still-open attempt

        # Attempt stays OK (no gateway_internal); Router stays OK.
        assert attempt.span.status == "OK"
        assert ATTR_ATTEMPT["error_category"] not in attempt.span.attributes
        assert handle.router.span.status == "OK"
        assert handle.router.success_count == 1
        assert handle.router.fail_count == 0
        # No response_failed for an already-successful attempt.
        assert len(_router_events(handle.router, "gateway.response.failed")) == 0

    def test_finalized_error_but_open_force_close_keeps_same_error(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        # Outcome aggregated as a business error (timeout), close() forgotten.
        handle.finish_attempt(attempt, error=TimeoutError("upstream timed out"))
        assert attempt._aggregated_to_router is True
        assert handle.router.fail_count == 1

        handle.finalize()

        # Attempt keeps the business error (timeout), NOT gateway_internal.
        assert attempt.span.status == "ERROR"
        assert attempt.span.attributes[ATTR_ATTEMPT["error_category"]] == (
            ErrorCategory.TIMEOUT
        )
        assert handle.router.span.attributes[ATTR_ROUTER["final_error_category"]] == (
            ErrorCategory.TIMEOUT
        )

    def test_finalized_open_attempt_no_duplicate_aggregation(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200, duration_ms=5.0)
        success_before = handle.router.success_count
        fail_before = handle.router.fail_count

        handle.finalize()  # force_close on an aggregated attempt → no re-aggregation

        assert handle.router.success_count == success_before
        assert handle.router.fail_count == fail_before

    def test_finalized_open_attempt_no_duplicate_report(self, tracer):
        spy = _ReportSpy(tracer)
        try:
            runtime = GatewayRuntime(tracer=tracer)
            handle = runtime.handle_request({})
            attempt = handle.start_attempt()
            attempt.start()
            handle.finish_attempt(attempt, upstream_status=200, duration_ms=5.0)
            attempt.close()
            span_id = attempt.span.span_id
            handle.finalize()
            # The attempt is reported exactly once (finalize must not re-report it).
            attempt_reports = [
                r for r in spy.records
                if isinstance(r, dict) and r.get("span_id") == span_id
            ]
            assert len(attempt_reports) == 1
        finally:
            spy.restore(tracer)
