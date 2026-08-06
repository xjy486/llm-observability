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
        assert GatewayContext.active_attempt() is attempt
        # Simulate the race: force-close without clearing this thread's token
        # (as if set_attempt ran after force_close). With the weakref slot,
        # marking _closed=True lazily invalidates the slot on read — no stale
        # ended Attempt is surfaced, even before close().
        attempt._closed = True
        assert GatewayContext.active_attempt() is None, (
            "ended Attempt must be lazily hidden from the context"
        )
        attempt.close()  # early-return path: idempotent, no token leak
        assert GatewayContext.active_attempt() is None, "owned ContextVar must stay cleared on close early-return"
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


class TestSetAttemptWindowRace:
    """Deterministic tests that block *inside* GatewayContext.set_attempt — i.e.
    the Attempt has already passed the closed-check and is inside the
    _lifecycle_lock critical section. This is the exact window the prior
    lockless second-check could not close; the lifecycle lock must hold.

    finalize() runs in its OWN thread because it blocks on _lifecycle_lock
    (held by set_attempt) until set_attempt returns; running it inline would
    deadlock the test thread against the worker.
    """

    def _patched_set_attempt_factory(self, entered, release, real_set_attempt):
        def patched_set_attempt(attempt):
            entered.set()
            release.wait(timeout=5)
            return real_set_attempt(attempt)
        return patched_set_attempt

    def _run_window_race(self, tracer, on_finalized):
        """Shared harness: start attempt → block inside set_attempt → finalize
        in a background thread (blocks on the lock) → release → join both.

        finalize() runs in its OWN thread because it blocks on _lifecycle_lock
        (held by set_attempt) until set_attempt returns; running it inline would
        deadlock the test thread against the worker. We wait until the finalizer
        is blocked on the lock (via a patched force_close signal) before
        releasing set_attempt, so force_close is guaranteed to win the lock
        next — the post-install re-check then sees _closed and clears the
        worker's own token.
        """
        import threading
        import time as _time
        from llm_observability.gateway_observability import context as ctx_mod

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        entered = threading.Event()
        release = threading.Event()
        force_close_waiting = threading.Event()
        real_set_attempt = ctx_mod.GatewayContext.set_attempt
        ctx_mod.GatewayContext.set_attempt = staticmethod(
            self._patched_set_attempt_factory(entered, release, real_set_attempt)
        )
        # Signal when force_close has begun (it will block on _lifecycle_lock
        # immediately after, since set_attempt still holds it).
        real_force_close = handle.router._force_close_open_attempts

        def signaled_force_close():
            force_close_waiting.set()
            return real_force_close()

        handle.router._force_close_open_attempts = signaled_force_close
        finalized_event = threading.Event()
        state = {}
        try:
            def starter():
                a = handle.start_attempt()
                a.start()
                state["attempt"] = a
                # Read the dereferenced active attempt AFTER finalize completes
                # (weakref lazy-invalidation: an ended Attempt reads None on
                # the worker thread even with no business close()).
                finalized_event.wait(timeout=5)
                state["ctx_after"] = GatewayContext.active_attempt()

            def finalizer():
                handle.finalize()
                state["finalized"] = True
                finalized_event.set()

            t = threading.Thread(target=starter)
            t.start()
            assert entered.wait(timeout=5), "did not reach the set_attempt window"
            f = threading.Thread(target=finalizer)
            f.start()
            # Wait until force_close is about to acquire _lifecycle_lock (it is
            # blocked because set_attempt still holds it). Now releasing
            # set_attempt guarantees force_close wins the lock next.
            assert force_close_waiting.wait(timeout=5), "force_close did not start"
            _time.sleep(0.02)  # let it actually block on the lock
            release.set()
            t.join(timeout=5)
            f.join(timeout=5)
            on_finalized(handle, state)
        finally:
            ctx_mod.GatewayContext.set_attempt = staticmethod(real_set_attempt)
            handle.router._force_close_open_attempts = real_force_close

    def test_finalize_after_closed_check_before_set_attempt_no_leak(self, tracer):
        from llm_observability.gateway_observability.events import EVENT_ATTEMPT_STARTED

        def check(handle, state):
            a = state["attempt"]
            assert a._closed is True, "attempt must be force-closed"
            assert state["ctx_after"] is None, "active_attempt must be None (post-install re-check cleared it)"
            assert GatewayContext.get().active_attempt is None
            started = [e for e in (a.span.events if a.span is not None else [])
                       if e.get("name") == EVENT_ATTEMPT_STARTED]
            assert started == [], "no late attempt.started on ended span"
        self._run_window_race(tracer, check)

    def test_no_started_event_when_force_close_occurs_inside_set_attempt_window(self, tracer):
        from llm_observability.gateway_observability.events import EVENT_ATTEMPT_STARTED

        def check(handle, state):
            a = state["attempt"]
            assert a._closed is True
            started = [e for e in (a.span.events if a.span is not None else [])
                       if e.get("name") == EVENT_ATTEMPT_STARTED]
            assert started == [], "attempt.started must not be recorded when force_close lands in the set_attempt window"
        self._run_window_race(tracer, check)

    def test_owner_never_calls_close_after_race_context_still_empty(self, tracer):
        # The decisive test: after the set_attempt-window race, do NOT call
        # attempt.close(). The leaked token must NOT survive — cleanup must
        # come from the lifecycle lock + post-install re-check, not from a
        # later business-thread close().
        def check(handle, state):
            assert state["ctx_after"] is None, (
                "active_attempt must be None without a business close() — "
                "cleanup comes from the lifecycle lock, not the owner"
            )
        self._run_window_race(tracer, check)


class TestCrossThreadFullActivationWeakRef:
    """Blocker (final): a fully-activated Attempt that is force-closed from
    ANOTHER thread (owner never calls close()) leaves no stale active_attempt
    on the owner / reused worker thread. ContextVars are per-thread, so
    cross-thread token reset is impossible — the weakref slot is lazily
    invalidated on read instead."""

    def test_cross_thread_force_close_after_full_activation_clears_owner_context(self, tracer):
        import threading
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        # Full activation: ContextVar installed, started recorded, lock released.
        assert GatewayContext.active_attempt() is attempt

        # Owner never calls close(); finalize (force_close) runs on another thread.
        finalizer = threading.Thread(target=handle.finalize)
        finalizer.start()
        finalizer.join()

        assert attempt._closed is True, "attempt must be force-closed"
        # The owner thread re-reads the context — the ended Attempt is lazily
        # hidden (no cross-thread token reset needed).
        assert GatewayContext.active_attempt() is None, (
            "ended Attempt must not surface on the owner thread"
        )

    def test_owner_never_closes_after_full_activation_context_not_stale(self, tracer):
        import threading
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        assert GatewayContext.active_attempt() is attempt

        finalizer = threading.Thread(target=handle.finalize)
        finalizer.start()
        finalizer.join()

        # Runtime.active_attempt() must never return an ended Attempt.
        assert runtime.active_attempt() is None
        assert GatewayContext.active_attempt() is None

    def test_thread_pool_worker_reused_after_force_close_has_no_active_attempt(self, tracer):
        import concurrent.futures
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        # A single-worker thread pool: Task 1 fully activates an Attempt, gets
        # force-closed from the main thread (owner never closes), then Task 2
        # runs on the SAME worker and reads the context.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        results = {}

        def task1():
            handle = runtime.handle_request({})
            attempt = handle.start_attempt()
            attempt.start()
            results["attempt"] = attempt
            results["active_after_start"] = GatewayContext.active_attempt() is attempt
            # Do NOT close — signal the main thread to finalize from outside.
            results["task1_done"] = True
            # Block here so the worker is reused for task2 on the same thread
            # only after finalize completes.
            import time as _time
            deadline = _time.monotonic() + 5
            while not results.get("finalized") and _time.monotonic() < deadline:
                _time.sleep(0.01)

        def task2():
            results["active_on_reuse"] = GatewayContext.active_attempt()
            results["runtime_active_on_reuse"] = runtime.active_attempt()

        # Run task1 on the single worker.
        f1 = pool.submit(task1)
        # Wait until task1 has activated + signalled, then finalize from here.
        import time as _time
        deadline = _time.monotonic() + 5
        while not results.get("task1_done") and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert results.get("task1_done"), "task1 did not activate"
        attempt = results["attempt"]
        # Force-close from the MAIN thread (cross-thread finalize).
        attempt.force_close()
        results["finalized"] = True
        f1.result(timeout=5)

        # Now reuse the same worker for task2 — it must NOT see a stale Attempt.
        f2 = pool.submit(task2)
        f2.result(timeout=5)
        pool.shutdown(wait=True)

        assert results["active_after_start"] is True, "task1 should have activated"
        assert results["active_on_reuse"] is None, (
            "reused worker must NOT see a stale ended active_attempt"
        )
        assert results["runtime_active_on_reuse"] is None


class TestCrossThreadRouterFinalizeWeakRef:
    """P0-1: the Router slot is weak + lazily invalidated, so a Router finalized
    from ANOTHER thread (owner never closes) does not leave a stale
    active_router (nor a transitive Attempt pin) on the owner / reused worker."""

    def test_cross_thread_router_finalize_hides_closed_router(self, tracer):
        import threading
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        router = handle.router
        # Router slot installed on this (owner) thread.
        assert runtime.active_router() is router

        # finalize from another thread.
        finalizer = threading.Thread(target=handle.finalize)
        finalizer.start()
        finalizer.join()
        assert router._closed is True

        # Owner re-reads: the ended Router is lazily hidden.
        assert runtime.active_router() is None, "ended Router must not surface on the owner thread"
        assert GatewayContext.get().router is None

    def test_thread_pool_worker_reuse_has_no_active_router(self, tracer):
        import concurrent.futures
        import time as _time
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        results = {}

        def task1():
            handle = runtime.handle_request({})
            attempt = handle.start_attempt()
            attempt.start()
            results["router"] = handle.router
            results["attempt"] = attempt
            results["active_router_after_start"] = runtime.active_router() is handle.router
            results["task1_done"] = True
            # Do NOT close — block until the main thread finalizes from outside.
            deadline = _time.monotonic() + 5
            while not results.get("finalized") and _time.monotonic() < deadline:
                _time.sleep(0.01)

        def task2():
            results["active_router_on_reuse"] = runtime.active_router()
            results["active_attempt_on_reuse"] = runtime.active_attempt()
            results["ctx_router_on_reuse"] = GatewayContext.get().router

        f1 = pool.submit(task1)
        deadline = _time.monotonic() + 5
        while not results.get("task1_done") and _time.monotonic() < deadline:
            _time.sleep(0.01)
        assert results.get("task1_done"), "task1 did not activate"
        handle_router = results["router"]
        # Cross-thread finalize (handle.finalize) from the MAIN thread.
        handle_router.close()  # Router.close() force-closes the open attempt too
        results["finalized"] = True
        f1.result(timeout=5)

        f2 = pool.submit(task2)
        f2.result(timeout=5)
        pool.shutdown(wait=True)

        assert results["active_router_after_start"] is True
        assert results["active_router_on_reuse"] is None, "reused worker must NOT see a stale Router"
        assert results["active_attempt_on_reuse"] is None
        assert results["ctx_router_on_reuse"] is None

    def test_runtime_active_router_never_returns_closed_router(self, tracer):
        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        router = handle.router
        assert runtime.active_router() is router
        # White-box: mark closed without clearing this thread's token.
        router._closed = True
        assert runtime.active_router() is None, "closed Router must be lazily hidden"
        handle.finalize()

    def test_cross_thread_finalize_releases_router_and_attempt_references(self, tracer):
        import gc
        import threading
        import weakref
        from llm_observability.gateway_observability.context import GatewayContext

        runtime = GatewayRuntime(tracer=tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        router = handle.router

        router_weak = weakref.ref(router)
        attempt_weak = weakref.ref(attempt)
        # Drop our strong local refs; only the context + handle hold them.
        del router
        del attempt

        finalizer = threading.Thread(target=handle.finalize)
        finalizer.start()
        finalizer.join()
        # Clear this thread's context too (lazy invalidation already hid it,
        # but force a clear so no strong ref lingers on the ContextVar).
        GatewayContext.clear()
        del handle
        gc.collect()

        # The Router (and its _attempts → Attempts) are now collectable.
        assert router_weak() is None, "ended Router must be collectable after finalize"
        assert attempt_weak() is None, "ended Attempt must be collectable after finalize"
