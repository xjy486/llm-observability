"""P0-5: Router finalize must force-close leftover open Attempts.

Covers:
- finalize() with open Attempts ends their spans (never just drops registry
  entries), marks them gateway_internal/router_finalized_with_open_attempt,
  and leaves registries + context empty.
- force_close is idempotent and never overwrites a recorded business error.
- No duplicate reports.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability.attributes import ATTR_ATTEMPT
from llm_observability.gateway_observability.context import (
    GatewayContext,
    clear_gateway_context,
)
from llm_observability.gateway_observability.errors import ErrorCategory, GatewayError
from llm_observability.gateway_observability.runtime import GatewayRuntime


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


def _reported_spans(tracer):
    return list(tracer.reporter._queue)


class _ReportSpy:
    """Synchronous recorder replacing the async reporter (queue is drained
    by a background thread, so counting _queue directly is racy)."""

    def __init__(self, tracer):
        self.records = []
        self._original = tracer.reporter.report
        tracer.reporter.report = self._capture

    def _capture(self, record):
        self.records.append(record)

    def restore(self, tracer):
        tracer.reporter.report = self._original


class TestRouterFinalizeForceClose:
    def test_router_finalize_force_closes_open_attempt(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        assert handle.router.open_attempt_count == 1

        handle.finalize()

        assert attempt._closed is True
        assert attempt.span is not None and attempt.span.end_time is not None
        assert attempt.span.attributes[ATTR_ATTEMPT["error_category"]] == ErrorCategory.GATEWAY_INTERNAL
        assert attempt.span.attributes[ATTR_ATTEMPT["error_message"]] == "router_finalized_with_open_attempt"
        assert attempt.span.status == "ERROR"

    def test_router_finalize_multiple_open_attempts(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempts = []
        for _ in range(3):
            a = handle.start_attempt()
            a.start()
            attempts.append(a)
        handle.finalize()
        for a in attempts:
            assert a._closed is True
            assert a.span.end_time is not None
        assert handle.router.open_attempt_count == 0

    def test_exception_between_attempt_start_and_close_no_leak(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        # Business explodes; attempt.close() never runs.
        try:
            raise RuntimeError("business boom")
        except RuntimeError:
            pass
        handle.finalize()

        assert runtime.attempt_registry.size() == 0, "attempt registry must be empty"
        assert runtime.router_registry.size() == 0, "router registry must be empty"
        state = GatewayContext.get()
        assert state.router is None and state.active_attempt is None

    def test_force_close_is_idempotent(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        attempt.force_close()
        first_end = attempt.span.end_time
        attempt.force_close()
        attempt.force_close(reason="other")
        assert attempt.span.end_time == first_end
        handle.finalize()

    def test_force_close_does_not_overwrite_business_error(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        attempt.set_error(GatewayError(
            category=ErrorCategory.TIMEOUT, type="TimeoutError",
            message="upstream timed out", retryable=True,
        ))
        handle.finalize()  # force-closes the open attempt
        assert attempt.span.attributes[ATTR_ATTEMPT["error_category"]] == ErrorCategory.TIMEOUT
        assert attempt.span.attributes[ATTR_ATTEMPT["error_message"]] == "upstream timed out"

    def test_router_finalize_after_attempt_end_does_not_duplicate_report(self, tracer):
        spy = _ReportSpy(tracer)
        try:
            runtime = GatewayRuntime(tracer=tracer)
            handle = runtime.handle_request({})
            attempt = handle.start_attempt()
            attempt.start()
            handle.finish_attempt(attempt, upstream_status=200, duration_ms=12.0)
            attempt.close()
            before = len(spy.records)
            handle.finalize()
            # Exactly one more report (the Router); the ended Attempt is not
            # reported again.
            assert len(spy.records) == before + 1
            attempt_reports = [
                r for r in spy.records
                if isinstance(r, dict) and r.get("span_id") == attempt.span.span_id
            ]
            assert len(attempt_reports) == 1
        finally:
            spy.restore(tracer)

    def test_open_attempt_registry_empty_after_router_finalize(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt()
        a1.start()
        a2 = handle.start_attempt()
        a2.start()
        handle.finalize()
        assert handle.router.open_attempt_count == 0
        assert handle.router.open_attempts == []
        assert runtime.attempt_registry.size() == 0


class TestOpenAttemptRegistryConcurrency:
    """P1-4: the _open_attempts registry is race-free under concurrency."""

    def test_concurrent_attempt_register_unregister_safe(self, tracer):
        import threading

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        started = []
        stop = threading.Event()

        def worker():
            while not stop.is_set():
                a = handle.start_attempt()
                a.start()
                a.close()
                started.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        # Let them run briefly, then stop and finalize.
        import time as _time
        _time.sleep(0.1)
        stop.set()
        for t in threads:
            t.join()

        handle.finalize()
        # No leaked open entries; every attempt that started also closed.
        assert handle.router.open_attempt_count == 0
        assert handle.router.open_attempts == []

    def test_finalize_snapshot_stable_under_concurrent_close(self, tracer):
        import threading

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempts = []
        # Start many open attempts, then race their close against Router finalize.
        for _ in range(20):
            a = handle.start_attempt()
            a.start()
            attempts.append(a)

        closed_flag = threading.Event()

        def closer():
            for a in attempts:
                if closed_flag.is_set():
                    return
                try:
                    a.close()
                except Exception:
                    pass

        closer_t = threading.Thread(target=closer)
        closer_t.start()
        handle.finalize()  # force-closes whatever is still open
        closed_flag.set()
        closer_t.join()

        # The registry is empty; each attempt closed exactly once (no double).
        assert handle.router.open_attempt_count == 0
        # fail_count reflects force-closed attempts only (those not already closed).
        reported = list(tracer.reporter._queue)
        attempt_span_ids = {a.span.span_id for a in attempts}
        for sid in attempt_span_ids:
            reports = [r for r in reported if isinstance(r, dict) and r.get("span_id") == sid]
            assert len(reports) == 1, f"attempt {sid} reported {len(reports)} times"


class TestClosedRouterRejectsRegistration:
    """Blocker 2 (follow-up): a closed Router rejects new Attempt registration."""

    def test_attempt_start_after_router_close_is_noop(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        handle.finalize()  # Router is now closed
        assert handle.router._closed is True

        # Starting an attempt after close is a fail-open no-op.
        attempt = handle.start_attempt()
        attempt.start()

        assert attempt._no_op is True
        # The span (if created) is never ended (end_time == 0 sentinel).
        if attempt.span is not None:
            assert attempt.span.end_time == 0
        # No registry entry, no active attempt.
        assert handle.router.open_attempt_count == 0
        assert runtime.attempt_registry.size() == 0
        from llm_observability.gateway_observability.context import GatewayContext
        state = GatewayContext.get()
        assert state.active_attempt is None
        # Closing the no-op attempt does not report or raise.
        attempt.close()
        if attempt.span is not None:
            assert attempt.span.end_time == 0, "no-op span must not be ended"

    def test_router_finalize_blocks_post_snapshot_registration(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        # Finalize closes the Router; a later register must be rejected.
        handle.finalize()
        assert handle.router.register_open_attempt.__call__  # method exists
        # A direct register call returns False (rejected).
        class _FakeAttempt:
            class _span:
                span_id = "fake-span-id"
            span = _span()
        ok = handle.router.register_open_attempt(_FakeAttempt())
        assert ok is False
        assert handle.router.open_attempt_count == 0

    def test_attempt_register_racing_router_finalize_no_leak(self, tracer):
        import threading

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        barrier = threading.Barrier(2)
        registered_after_close = {"count": 0}

        def starter():
            barrier.wait()
            for _ in range(50):
                a = handle.start_attempt()
                a.start()
                if a._no_op:
                    registered_after_close["count"] += 1
                else:
                    a.close()

        starter_t = threading.Thread(target=starter)
        starter_t.start()
        barrier.wait()
        handle.finalize()
        starter_t.join()

        # After finalize the registry is empty regardless of the race.
        assert handle.router.open_attempt_count == 0
        assert runtime.attempt_registry.size() == 0

    def test_concurrent_attempt_start_and_finalize_registry_zero(self, tracer):
        import threading

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        stop = threading.Event()
        started_any = {"count": 0}

        def starter():
            while not stop.is_set():
                a = handle.start_attempt()
                a.start()
                if not a._no_op:
                    started_any["count"] += 1
                    a.close()

        threads = [threading.Thread(target=starter) for _ in range(6)]
        for t in threads:
            t.start()
        import time as _time
        _time.sleep(0.1)
        handle.finalize()
        stop.set()
        for t in threads:
            t.join()

        # No leaked open entries after finalize.
        assert handle.router.open_attempt_count == 0
        assert runtime.attempt_registry.size() == 0


class TestAttemptActivationRace:
    """Deterministic barrier tests for the activate→set_attempt / allocation
    races (fix-gateway-attempt-activation-race). Patches ``activate_attempt``
    to hit the exact window between activation and ContextVar setup."""

    def test_finalize_after_register_before_context_set_no_context_leak(self, tracer):
        import threading
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        router = handle.router
        activated = threading.Event()
        release = threading.Event()
        orig_activate = router.activate_attempt

        def patched_activate(attempt):
            result = orig_activate(attempt)
            # Block AFTER activation (registered + indexed) but BEFORE start()
            # calls set_attempt — the exact race window.
            activated.set()
            release.wait(timeout=5)
            return result

        router.activate_attempt = patched_activate
        results = {}

        def starter():
            a = handle.start_attempt()
            a.start()
            results["attempt"] = a
            results["ctx_after"] = GatewayContext.get().active_attempt

        t = threading.Thread(target=starter)
        t.start()
        assert activated.wait(timeout=5), "attempt did not reach the activation window"
        # While the attempt is activated-but-not-active, finalize the Router:
        # force_close ends the attempt (sets _closed, cleans registry).
        handle.finalize()
        release.set()  # let start() resume into the second-check
        t.join()

        a = results["attempt"]
        assert a._closed is True, "attempt must be force-closed by finalize"
        assert results["ctx_after"] is None, "active_attempt must NOT be set on a force-closed attempt"
        assert GatewayContext.get().active_attempt is None

    def test_no_started_event_after_attempt_force_closed(self, tracer):
        import threading
        from llm_observability.gateway_observability.events import EVENT_ATTEMPT_STARTED

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        router = handle.router
        activated = threading.Event()
        release = threading.Event()
        orig_activate = router.activate_attempt

        def patched_activate(attempt):
            result = orig_activate(attempt)
            activated.set()
            release.wait(timeout=5)
            return result

        router.activate_attempt = patched_activate
        attempt_holder = {}

        def starter():
            a = handle.start_attempt()
            a.start()
            attempt_holder["attempt"] = a

        t = threading.Thread(target=starter)
        t.start()
        assert activated.wait(timeout=5)
        handle.finalize()
        release.set()
        t.join()

        a = attempt_holder["attempt"]
        assert a._closed is True
        started = [e for e in (a.span.events if a.span is not None else []) if e.get("name") == EVENT_ATTEMPT_STARTED]
        assert started == [], "attempt.started must not be recorded on a force-closed attempt"

    def test_force_close_in_other_thread_then_owner_close_clears_context(self, tracer):
        # White-box: construct the state "attempt is closed but still owns the
        # ContextVar token" (the exact leak the second-check prevents at the
        # source) and assert close()'s early-return still clears it.
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        assert GatewayContext.get().active_attempt is attempt
        # Simulate the race: force-close without clearing this thread's token
        # (as if set_attempt ran after force_close).
        attempt._closed = True
        assert GatewayContext.get().active_attempt is attempt
        attempt.close()  # early-return path must clear the owned token
        assert GatewayContext.get().active_attempt is None, "owned ContextVar must be cleared on close early-return"
        handle.finalize()

    def test_rejected_attempt_does_not_increment_attempt_count(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        before = handle.router.attempt_count
        handle.finalize()  # Router now closed
        a = handle.start_attempt()
        a.start()
        assert a._no_op is True
        assert handle.router.attempt_count == before, "rejected attempt must not bump attempt_count"

    def test_attempt_allocation_racing_finalize_not_in_router_count(self, tracer):
        import threading

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        router = handle.router
        before_activate = threading.Event()
        release = threading.Event()
        orig_activate = router.activate_attempt

        def patched_activate(attempt):
            # Block BEFORE the original runs (before acquiring the lock / seeing
            # _closed). finalize can therefore proceed and set _closed first.
            before_activate.set()
            release.wait(timeout=5)
            return orig_activate(attempt)

        router.activate_attempt = patched_activate
        before_count = router.attempt_count
        before_used = set(router._used_attempt_indices)
        attempt_holder = {}

        def starter():
            a = handle.start_attempt()
            a.start()
            attempt_holder["attempt"] = a

        t = threading.Thread(target=starter)
        t.start()
        assert before_activate.wait(timeout=5), "did not reach the activation window"
        handle.finalize()  # Router now closed while start() is blocked pre-activate
        release.set()  # activate_attempt proceeds → sees _closed → False → no-op
        t.join()

        assert router.attempt_count == before_count, "no index allocated for a rejected attempt"
        assert router._used_attempt_indices == before_used
        a = attempt_holder["attempt"]
        assert a._no_op is True

    def test_worker_thread_context_empty_after_finalize_race(self, tracer):
        import threading
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        router = handle.router
        activated = threading.Event()
        release = threading.Event()
        orig_activate = router.activate_attempt
        ctx_on_worker = {}

        def patched_activate(attempt):
            result = orig_activate(attempt)
            activated.set()
            release.wait(timeout=5)
            return result

        router.activate_attempt = patched_activate

        def worker():
            a = handle.start_attempt()
            a.start()
            ctx_on_worker["after"] = GatewayContext.get().active_attempt
            a.close()
            ctx_on_worker["after_close"] = GatewayContext.get().active_attempt

        t = threading.Thread(target=worker)
        t.start()
        assert activated.wait(timeout=5)
        handle.finalize()
        release.set()
        t.join()

        assert ctx_on_worker["after"] is None, "worker active_attempt must be None (force-closed)"
        assert ctx_on_worker["after_close"] is None, "worker active_attempt must stay None after close"
