"""P0-2: Hedged / Parallel Attempt Winner semantics (spec §13.4).

The Router's final status / channel / HTTP status / error are determined by an
explicit business Winner (``select_winner``), NOT by the last-completing
attempt. Usage/Cost still aggregate every attempt (including losing hedged
attempts). With no explicit Winner, a deterministic fail-safe applies
(auto-single-success / auto-single-attempt / ``MissingWinnerSelection``).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    AttemptResult,
    NormalizedUsage,
    NormalizedCost,
    ErrorCategory,
    GatewayError,
)
from llm_observability.gateway_observability.attributes import ATTR_ROUTER
from llm_observability.gateway_observability.context import clear_gateway_context
from llm_observability.gateway_observability.events import EVENT_ATTEMPT_SELECTED
from llm_observability.gateway_observability.runtime import GatewayRuntime


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


def _u(inp, out):
    return NormalizedUsage(input_tokens=inp, output_tokens=out, total_tokens=inp + out)


def _c(inp, out):
    return NormalizedCost(input_cost=inp, output_cost=out, total_cost=inp + out, cost_source="priced")


def _router(tracer):
    return GatewayRuntime(tracer=tracer, sample_rate=1.0)


def _events(span, name):
    return [e for e in span.events if e["name"] == name]


class TestHedgedWinner:
    def test_hedged_success_then_loser_timeout_router_stays_ok(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        # Attempt 1 succeeds (the business-adopted winner); attempt 2 is the
        # parallel loser that times out later.
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-1", http_status_code=200,
            usage=_u(10, 5), cost=_c(1.0, 2.0), success=True,
        ))
        router.register_attempt_result(AttemptResult(
            attempt_index=2, channel_id="ch-2", http_status_code=504,
            error=GatewayError(category=ErrorCategory.TIMEOUT, type="TimeoutError",
                               message="timed out", retryable=True),
            usage=_u(3, 0), cost=_c(0.5, 0.0), success=False,
        ))
        assert router.select_winner(attempt_index=1, reason="first_success") is True
        handle.finalize()
        # Router stays OK — the loser's timeout does NOT override the winner.
        assert router.span.status == "OK"
        assert router.final_channel_id == "ch-1"
        assert router.final_http_status == 200
        assert router.final_error is None

    def test_hedged_loser_timeout_then_success_router_ok(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        # Loser times out first, then the winner succeeds.
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-2", http_status_code=504,
            error=GatewayError(category=ErrorCategory.TIMEOUT, type="TimeoutError",
                               message="timed out", retryable=True),
            success=False,
        ))
        router.register_attempt_result(AttemptResult(
            attempt_index=2, channel_id="ch-1", http_status_code=200,
            success=True,
        ))
        assert router.select_winner(attempt_index=2, reason="first_success") is True
        handle.finalize()
        assert router.span.status == "OK"
        assert router.final_channel_id == "ch-1"
        assert router.final_http_status == 200

    def test_selected_attempt_defines_final_channel(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-a", http_status_code=200, success=True,
        ))
        router.register_attempt_result(AttemptResult(
            attempt_index=2, channel_id="ch-b", http_status_code=200, success=True,
        ))
        assert router.select_winner(attempt_index=2) is True
        handle.finalize()
        assert router.final_channel_id == "ch-b"
        # Span attribute is the hashed channel, never the raw id.
        assert router.span.attributes[ATTR_ROUTER["channel_id"]] == router._privacy.hash_channel_id("ch-b")

    def test_selected_attempt_defines_final_http_status(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-a", http_status_code=201, success=True,
        ))
        router.register_attempt_result(AttemptResult(
            attempt_index=2, channel_id="ch-b", http_status_code=500,
            error=GatewayError(category=ErrorCategory.PROVIDER_5XX, type="HTTP500",
                               message="err", retryable=True),
            success=False,
        ))
        assert router.select_winner(attempt_index=1) is True
        handle.finalize()
        assert router.final_http_status == 201

    def test_selected_attempt_defines_final_error(self, tracer):
        # All attempts failed; the business layer surfaces attempt 1's failure.
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-a", http_status_code=504,
            error=GatewayError(category=ErrorCategory.TIMEOUT, type="TimeoutError",
                               message="timed out", retryable=True),
            success=False,
        ))
        router.register_attempt_result(AttemptResult(
            attempt_index=2, channel_id="ch-b", http_status_code=502,
            error=GatewayError(category=ErrorCategory.PROVIDER_5XX, type="HTTP502",
                               message="bad gateway", retryable=True),
            success=False,
        ))
        assert router.select_winner(attempt_index=1, reason="surfaced_failure") is True
        handle.finalize()
        assert router.span.status == "ERROR"
        assert router.final_error is not None
        assert router.final_error.category == ErrorCategory.TIMEOUT
        assert router.final_http_status == 504

    def test_parallel_usage_includes_all_attempts(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-1", http_status_code=200,
            usage=_u(10, 5), success=True,
        ))
        router.register_attempt_result(AttemptResult(
            attempt_index=2, channel_id="ch-2", http_status_code=504,
            error=GatewayError(category=ErrorCategory.TIMEOUT, type="T", message="m", retryable=True),
            usage=_u(7, 0), success=False,
        ))
        assert router.select_winner(attempt_index=1) is True
        handle.finalize()
        agg = router.usage_aggregate
        assert agg is not None
        # Winner + loser usage both aggregated.
        assert agg.input_tokens == 17
        assert agg.output_tokens == 5
        assert agg.total_tokens == 22

    def test_parallel_cost_includes_losing_attempts(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-1", http_status_code=200,
            cost=_c(1.0, 2.0), success=True,
        ))
        router.register_attempt_result(AttemptResult(
            attempt_index=2, channel_id="ch-2", http_status_code=504,
            error=GatewayError(category=ErrorCategory.TIMEOUT, type="T", message="m", retryable=True),
            cost=_c(0.5, 0.0), success=False,
        ))
        assert router.select_winner(attempt_index=1) is True
        handle.finalize()
        agg = router.cost_aggregate
        assert agg is not None
        # Winner + loser (hedge waste) cost both aggregated.
        assert agg.input_cost == pytest.approx(1.5)
        assert agg.output_cost == pytest.approx(2.0)
        assert agg.total_cost == pytest.approx(3.5)

    def test_select_winner_is_idempotent(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-1", http_status_code=200, success=True,
        ))
        assert router.select_winner(attempt_index=1, reason="first_success") is True
        assert router.select_winner(attempt_index=1, reason="first_success") is True
        handle.finalize()
        # Exactly one selection event for the same index.
        assert len(_events(router.span, EVENT_ATTEMPT_SELECTED)) == 1

    def test_select_unknown_attempt_rejected(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-1", http_status_code=200, success=True,
        ))
        # No result for index 99 → rejected, and no selection event is recorded
        # by the rejected call.
        assert router.select_winner(attempt_index=99) is False
        assert len(_events(router.span, EVENT_ATTEMPT_SELECTED)) == 0
        handle.finalize()

    def test_multiple_attempts_without_winner_is_deterministic(self, tracer):
        # Two hedged successes, no explicit Winner → ambiguous → the Router
        # MUST NOT implicitly pick the last-completing attempt. Deterministic
        # fail-safe: MissingWinnerSelection (gateway_internal), Router ERROR.
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        router.register_attempt_result(AttemptResult(
            attempt_index=1, channel_id="ch-1", http_status_code=200, success=True,
        ))
        router.register_attempt_result(AttemptResult(
            attempt_index=2, channel_id="ch-2", http_status_code=200, success=True,
        ))
        handle.finalize()
        assert router.span.status == "ERROR"
        assert router.final_error is not None
        assert router.final_error.category == ErrorCategory.GATEWAY_INTERNAL
        assert router.final_error.type == "MissingWinnerSelection"
        assert router.selected_attempt_index is None
        assert len(_events(router.span, "gateway.response.failed")) == 1
