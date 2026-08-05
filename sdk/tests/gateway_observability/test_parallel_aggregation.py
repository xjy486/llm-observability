"""P1: parallel attempt-result aggregation is race-free (aggregate lock).

Concurrent (hedged / parallel provider) attempts all call
``register_attempt_result``; the aggregate lock prevents lost count updates and
overwritten usage/cost read-modify-writes.
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
