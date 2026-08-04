"""P0-3 + P1-1: gateway context slot semantics + attempt index allocation.

Covers:
- Attempt close (normal/error/async/cross-context) clears ONLY the attempt
  slot — the Router context survives for retries/fallbacks/aggregation.
- Router close clears both slots.
- Default attempt indices increment; duplicates remap; invalid values fall
  back; concurrent allocation is unique.
"""
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability.context import (
    GatewayContext,
    clear_gateway_context,
)
from llm_observability.gateway_observability.runtime import GatewayRuntime
from llm_observability.gateway_observability.router_span import RouterSpan


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


class TestAttemptClosePreservesRouter:
    def test_attempt_close_preserves_active_router(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        assert runtime.active_attempt() is attempt
        attempt.close()
        assert runtime.active_attempt() is None
        assert runtime.active_router() is handle.router, "attempt close must keep the Router"
        handle.finalize()
        assert runtime.active_router() is None

    def test_attempt_error_preserves_active_router(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        try:
            with attempt:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert runtime.active_router() is handle.router
        handle.finalize()

    def test_retry_second_attempt_uses_same_router(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt()
        a1.start()
        a1.close()
        assert runtime.active_router() is handle.router
        handle.retry_scheduled(attempt_index=a1.attempt_index, delay_ms=1, reason="test")
        a2 = handle.start_attempt()
        a2.start()
        assert a2._router is handle.router
        assert a2.attempt_index != a1.attempt_index
        a2.close()
        assert runtime.active_router() is handle.router
        handle.finalize()

    def test_fallback_second_attempt_uses_same_router(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt()
        a1.start()
        a1.close()
        handle.fallback_selected(from_channel_id="ch-a", to_channel_id="ch-b", reason="timeout")
        a2 = handle.start_attempt()
        a2.start()
        assert a2._router is handle.router
        a2.close()
        handle.finalize()

    def test_attempt_close_clears_only_attempt_slot(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        attempt.close()
        state = GatewayContext.get()
        assert state.router is handle.router, "router slot must survive attempt close"
        assert state.active_attempt is None, "attempt slot must be cleared"
        handle.finalize()

    def test_router_close_clears_router_and_attempt_slots(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        handle.finalize()  # force-closes the open attempt, then the router
        state = GatewayContext.get()
        assert state.router is None
        assert state.active_attempt is None

    def test_async_attempt_close_preserves_router(self, tracer):
        async def run():
            runtime = GatewayRuntime(tracer=tracer)
            handle = await runtime.handle_request_async({})
            attempt = handle.start_attempt()
            attempt.start()
            attempt.close()
            assert runtime.active_router() is handle.router
            handle.finalize()
            assert runtime.active_router() is None
        asyncio.run(run())

    def test_cross_context_attempt_reset_preserves_router(self, tracer):
        """A token reset from a foreign asyncio Context must clear only the
        attempt slot — never the router slot."""
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        router = handle.router

        async def child():
            attempt = handle.start_attempt()
            attempt.start()
            attempt.close()  # token created in this child context
            return attempt

        attempt = asyncio.run(child())
        # In the main context the router slot must still be intact.
        state = GatewayContext.get()
        assert state.router is router
        handle.finalize()


class TestAttemptIndexAllocation:
    def test_default_attempt_index_increments(self, tracer):
        router = RouterSpan(tracer=tracer).start()
        a1 = router.attempt()
        a2 = router.attempt()
        a3 = router.attempt()
        assert (a1.attempt_index, a2.attempt_index, a3.attempt_index) == (1, 2, 3)
        router.close()

    def test_attempt_count_matches_actual_attempts(self, tracer):
        router = RouterSpan(tracer=tracer).start()
        router.attempt()
        router.attempt()
        assert router.attempt_count == 2
        router.attempt(attempt_index=7)  # explicit — still one more attempt
        assert router.attempt_count == 3
        router.close()

    def test_duplicate_explicit_attempt_index_handled(self, tracer):
        router = RouterSpan(tracer=tracer).start()
        a1 = router.attempt(attempt_index=2)
        a2 = router.attempt(attempt_index=2)  # duplicate → remapped
        assert a1.attempt_index == 2
        assert a2.attempt_index != 2
        indices = {a.attempt_index for a in router.attempts}
        assert len(indices) == 2, "duplicate explicit index must be remapped"
        router.close()

    def test_invalid_attempt_index_falls_back(self, tracer):
        router = RouterSpan(tracer=tracer).start()
        a_zero = router.attempt(attempt_index=0)
        a_neg = router.attempt(attempt_index=-3)
        a_str = router.attempt(attempt_index="x")
        for a in (a_zero, a_neg, a_str):
            assert isinstance(a.attempt_index, int)
            assert a.attempt_index >= 1
        indices = [a.attempt_index for a in router.attempts]
        assert len(set(indices)) == 3
        router.close()

    def test_parallel_attempt_index_is_thread_safe(self, tracer):
        router = RouterSpan(tracer=tracer).start()
        indices = []
        lock = threading.Lock()

        def worker():
            a = router.attempt()
            with lock:
                indices.append(a.attempt_index)

        threads = [threading.Thread(target=worker) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(indices) == 32
        assert len(set(indices)) == 32, f"duplicate indices under concurrency: {sorted(indices)}"
        router.close()
