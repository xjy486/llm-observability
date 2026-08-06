"""P1: parallel attempt-result aggregation + exactly-once single-Attempt funnel.

Concurrent (hedged / parallel provider) attempts all call
``register_attempt_result``; the aggregate lock prevents lost count updates and
overwritten usage/cost read-modify-writes. The single ``try_aggregate_result``
funnel guarantees the SAME Attempt is aggregated at most once across racing
paths (finalize_attempt / streaming finalizer / force_close).
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    AttemptResult,
    NormalizedUsage,
    NormalizedCost,
)
from llm_observability.gateway_observability.attributes import ATTR_ROUTER
from llm_observability.gateway_observability.context import clear_gateway_context
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


class TestParallelAggregation:
    def test_parallel_attempt_results_count_exact(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        n = 32

        def worker(i):
            router.register_attempt_result(AttemptResult(
                attempt_index=i + 1, channel_id="ch", http_status_code=200,
                usage=_u(10, 5), cost=_c(1.0, 2.0), success=True,
            ))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert router.success_count == n
        assert router.fail_count == 0

    def test_parallel_attempt_usage_sum_exact(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        n = 40  # each adds input=10, output=5 → total per = 15

        def worker(i):
            router.register_attempt_result(AttemptResult(
                attempt_index=i + 1, http_status_code=200,
                usage=_u(10, 5), success=True,
            ))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        agg = router.usage_aggregate
        assert agg is not None
        assert agg.input_tokens == 10 * n
        assert agg.output_tokens == 5 * n
        assert agg.total_tokens == 15 * n

    def test_parallel_attempt_cost_sum_exact(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        n = 40  # each adds input_cost=1.0, output_cost=2.0

        def worker(i):
            router.register_attempt_result(AttemptResult(
                attempt_index=i + 1, http_status_code=200,
                cost=_c(1.0, 2.0), success=True,
            ))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        agg = router.cost_aggregate
        assert agg is not None
        assert agg.input_cost == pytest.approx(1.0 * n)
        assert agg.output_cost == pytest.approx(2.0 * n)
        assert agg.total_cost == pytest.approx(3.0 * n)

    def test_parallel_success_failure_counts_exact(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        router = handle.router
        n_succ = 20
        n_fail = 12

        def succ(i):
            router.register_attempt_result(AttemptResult(
                attempt_index=i + 1, http_status_code=200, success=True,
            ))

        def fail(i):
            router.register_attempt_result(AttemptResult(
                attempt_index=n_succ + i + 1, http_status_code=500, success=False,
            ))

        threads = [threading.Thread(target=succ, args=(i,)) for i in range(n_succ)]
        threads += [threading.Thread(target=fail, args=(i,)) for i in range(n_fail)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert router.success_count == n_succ
        assert router.fail_count == n_fail


class TestSingleAttemptExactlyOnceAggregation:
    """P1: try_aggregate_result makes the same Attempt aggregate exactly once
    across racing paths (finalize_attempt / streaming finalizer / force_close)."""

    def test_finish_attempt_racing_force_close_aggregates_once(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        barrier = threading.Barrier(2)

        def finish():
            barrier.wait()
            # Business success result.
            handle.finish_attempt(attempt, upstream_status=200, duration_ms=5.0)

        def force():
            barrier.wait()
            attempt.force_close()

        tf = threading.Thread(target=finish)
        tforce = threading.Thread(target=force)
        tf.start(); tforce.start()
        tf.join(); tforce.join()
        attempt.close()
        handle.finalize()

        # Exactly one aggregation: success_count + fail_count == 1.
        assert handle.router.success_count + handle.router.fail_count == 1, (
            f"same Attempt aggregated {handle.router.success_count + handle.router.fail_count} times"
        )

    def test_stream_finalize_racing_router_finalize_aggregates_once(self, tracer):
        from llm_observability.gateway_observability import GatewayStream
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        chunks = iter([{"choices": [{"delta": {"content": "hi"}}]}])
        stream = GatewayStream(chunks, handle.router, attempt, runtime_handle=handle)

        barrier = threading.Barrier(2)

        def consume():
            barrier.wait()
            list(stream)  # drives finalize_success

        def finalize():
            barrier.wait()
            handle.finalize()  # may force_close if still open

        tc = threading.Thread(target=consume)
        tf = threading.Thread(target=finalize)
        tc.start(); tf.start()
        tc.join(); tf.join()

        # The Attempt was aggregated at most once (success or force_close, not both).
        total = handle.router.success_count + handle.router.fail_count
        assert total <= 1, f"same Attempt aggregated {total} times"

    def test_same_attempt_success_failure_race_count_equals_one(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        succ = AttemptResult(
            attempt_index=attempt.attempt_index, http_status_code=200,
            usage=_u(10, 5), success=True,
        )
        fail = AttemptResult(
            attempt_index=attempt.attempt_index, http_status_code=500,
            error=None, success=False,
        )
        barrier = threading.Barrier(2)

        def agg_succ():
            barrier.wait()
            attempt.try_aggregate_result(succ)

        def agg_fail():
            barrier.wait()
            attempt.try_aggregate_result(fail)

        t1 = threading.Thread(target=agg_succ)
        t2 = threading.Thread(target=agg_fail)
        t1.start(); t2.start()
        t1.join(); t2.join()
        attempt.close()
        handle.finalize()

        # Exactly one of the two won.
        assert handle.router.success_count + handle.router.fail_count == 1


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


class TestPublishBeforeClaim:
    """P0-2: try_aggregate_result publishes to the Router BEFORE claiming
    _aggregated_to_router, so a Router.finalize() that races the publish (or
    observes the claim) reports a Router Record that already includes the
    result — no result is published after the Router Report."""

    def test_router_finalize_waits_for_claimed_attempt_aggregation(self, tracer):
        import threading

        runtime = _router(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        publish_entered = threading.Event()
        publish_release = threading.Event()
        orig_publish = handle.router.register_attempt_result

        def blocked_publish(result):
            # Inside try_aggregate_result's lifecycle-lock CS, before the claim.
            publish_entered.set()
            publish_release.wait(timeout=5)
            return orig_publish(result)

        handle.router.register_attempt_result = blocked_publish
        results = {}

        def finisher():
            # finish_attempt aggregates a success result (publish blocked).
            handle.finish_attempt(attempt, upstream_status=200, duration_ms=5.0,
                                   raw_usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
            results["finished"] = True

        tf = threading.Thread(target=finisher)
        tf.start()
        assert publish_entered.wait(timeout=5), "did not reach the publish window"
        # finalize races: it must wait for the publish (claim not yet set).
        finalize_done = threading.Event()

        def finalizer():
            handle.finalize()
            finalize_done.set()

        tfinal = threading.Thread(target=finalizer)
        tfinal.start()
        # Let finalize block on the lifecycle lock (held by finish_attempt's CS).
        import time as _time
        _time.sleep(0.05)
        publish_release.set()  # finish_attempt publishes + claims, then exits CS
        tf.join(timeout=5)
        tfinal.join(timeout=5)
        attempt.close()

        # finalize reported the Router AFTER the publish, so the Router is OK
        # (the finalize completed without the result being lost).
        assert finalize_done.is_set()

    def test_error_result_published_before_router_report(self, tracer):
        import threading

        runtime = _router(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        spy = _ReportSpy(tracer)
        try:
            publish_entered = threading.Event()
            publish_release = threading.Event()
            orig_publish = handle.router.register_attempt_result

            def blocked_publish(result):
                publish_entered.set()
                publish_release.wait(timeout=5)
                return orig_publish(result)

            handle.router.register_attempt_result = blocked_publish

            def finisher():
                handle.finish_attempt(attempt, error=TimeoutError("upstream timed out"))

            tf = threading.Thread(target=finisher)
            tf.start()
            assert publish_entered.wait(timeout=5)
            finalize_done = threading.Event()

            def finalizer():
                handle.finalize()
                finalize_done.set()

            tfinal = threading.Thread(target=finalizer)
            tfinal.start()
            import time as _time
            _time.sleep(0.05)
            publish_release.set()
            tf.join(timeout=5)
            tfinal.join(timeout=5)
            attempt.close()

            rec = spy.router_record(handle.router)
            assert rec is not None, "Router must be reported"
            # The reported record carries the error category (publish-before-claim).
            assert rec["attributes"].get(ATTR_ROUTER["final_error_category"]) == "timeout"
        finally:
            spy.restore(tracer)

    def test_usage_cost_published_before_router_report(self, tracer):
        import threading

        runtime = _router(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        spy = _ReportSpy(tracer)
        try:
            publish_entered = threading.Event()
            publish_release = threading.Event()
            orig_publish = handle.router.register_attempt_result

            def blocked_publish(result):
                publish_entered.set()
                publish_release.wait(timeout=5)
                return orig_publish(result)

            handle.router.register_attempt_result = blocked_publish

            def finisher():
                handle.finish_attempt(attempt, upstream_status=200, duration_ms=5.0,
                                      raw_usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})

            tf = threading.Thread(target=finisher)
            tf.start()
            assert publish_entered.wait(timeout=5)
            finalize_done = threading.Event()

            def finalizer():
                handle.finalize()
                finalize_done.set()

            tfinal = threading.Thread(target=finalizer)
            tfinal.start()
            import time as _time
            _time.sleep(0.05)
            publish_release.set()
            tf.join(timeout=5)
            tfinal.join(timeout=5)
            attempt.close()

            rec = spy.router_record(handle.router)
            assert rec is not None
            # usage + cost landed before the Router was reported.
            assert rec["attributes"].get("usage.total_tokens") == 10
            assert rec["attributes"].get("cost.source") in ("priced", "unpriced")
        finally:
            spy.restore(tracer)

    def test_stream_finalize_race_aggregates_exactly_one(self, tracer):
        # The streaming-vs-router-finalize race must aggregate exactly ONE
        # (not zero, not two) — the prior test used assert <= 1 which let 0 pass.
        from llm_observability.gateway_observability import GatewayStream
        runtime = _router(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        chunks = iter([{"choices": [{"delta": {"content": "hi"}}]}])
        stream = GatewayStream(chunks, handle.router, attempt, runtime_handle=handle)

        barrier = threading.Barrier(2)

        def consume():
            barrier.wait()
            list(stream)

        def finalize():
            barrier.wait()
            handle.finalize()

        tc = threading.Thread(target=consume)
        tf = threading.Thread(target=finalize)
        tc.start(); tf.start()
        tc.join(); tf.join()

        total = handle.router.success_count + handle.router.fail_count
        assert total == 1, f"exactly one aggregation expected, got {total}"

    def test_router_report_record_matches_final_in_memory_aggregate(self, tracer):
        import threading

        runtime = _router(tracer)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt()
        attempt.start()
        spy = _ReportSpy(tracer)
        try:
            publish_entered = threading.Event()
            publish_release = threading.Event()
            orig_publish = handle.router.register_attempt_result

            def blocked_publish(result):
                publish_entered.set()
                publish_release.wait(timeout=5)
                return orig_publish(result)

            handle.router.register_attempt_result = blocked_publish

            def finisher():
                handle.finish_attempt(attempt, upstream_status=200, duration_ms=5.0,
                                      raw_usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})

            tf = threading.Thread(target=finisher)
            tf.start()
            assert publish_entered.wait(timeout=5)
            finalize_done = threading.Event()

            def finalizer():
                handle.finalize()
                finalize_done.set()

            tfinal = threading.Thread(target=finalizer)
            tfinal.start()
            import time as _time
            _time.sleep(0.05)
            publish_release.set()
            tf.join(timeout=5)
            tfinal.join(timeout=5)
            attempt.close()

            rec = spy.router_record(handle.router)
            assert rec is not None
            # The reported record matches the final in-memory state.
            assert rec["attributes"].get("usage.total_tokens") == 10
            assert rec["attributes"].get(ATTR_ROUTER["attempt_count"]) == 1
            assert rec["attributes"].get(ATTR_ROUTER["final_error_category"]) is None or \
                   rec["status"] == "OK"
            # In-memory agrees.
            assert handle.router.usage_aggregate.total_tokens == 10
            assert handle.router.attempt_count == 1
        finally:
            spy.restore(tracer)
