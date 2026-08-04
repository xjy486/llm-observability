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

from llm_observability.gateway_observability.attributes import ATTR_ROUTER
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
