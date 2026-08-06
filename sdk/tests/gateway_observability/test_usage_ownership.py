"""P0-7 + P1-2: usage/cost ownership tests (adversarial).

Covers:
- Attempt owns single-request usage/cost; Router aggregates ALL attempts
  (including failed retries); SDK LLM keeps only the logical response usage.
- No ContextVar write-back of the Router aggregate into the LLM span.
- Retry waste is derivable: Router aggregate − final successful attempt.
- Cost uses the resolved model; pricing unit is USD per 1M tokens; unknown
  model → unpriced; explicit cache cost preserved.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    CostCalculator,
    NormalizedCost,
    NormalizedUsage,
)
from llm_observability.gateway_observability.aggregation import (
    apply_router_usage_to_span,
    router_usage_for_llm,
)
from llm_observability.gateway_observability.context import clear_gateway_context


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


PRICING = {
    "gpt-5.6": {"input_usd_per_1m_tokens": 2.0, "output_usd_per_1m_tokens": 8.0},
}


def _runtime(tracer):
    return GatewayRuntime(
        tracer=tracer, sample_rate=1.0,
        cost_calculator=CostCalculator(pricing_table=PRICING),
    )


class TestUsageOwnership:
    def test_router_usage_is_sum_of_all_attempts(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a1.start()
        handle.finish_attempt(a1, error=TimeoutError("t"), raw_usage={
            "prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10,
        })
        a1.close()
        a2 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a2.start()
        handle.finish_attempt(a2, upstream_status=200, raw_usage={
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        })
        a2.close()
        handle.finalize()

        agg = handle.router.usage_aggregate
        assert agg.input_tokens == 20
        assert agg.output_tokens == 5
        assert agg.total_tokens == 25

    def test_llm_usage_remains_logical_response_usage(self, tracer):
        """The aggregation hook must NOT write the Router aggregate anywhere."""
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a1.start()
        handle.finish_attempt(a1, upstream_status=200, raw_usage={
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        })
        a1.close()

        class FakeLLMSpan:
            def __init__(self):
                self.attributes = {"gen_ai.usage.input_tokens": 10}
            def set_attribute(self, k, v):
                self.attributes[k] = v

        span = FakeLLMSpan()
        assert router_usage_for_llm() is None
        assert apply_router_usage_to_span(span) is False
        assert span.attributes == {"gen_ai.usage.input_tokens": 10}, "logical usage untouched"
        handle.finalize()

    def test_failed_retry_usage_counted_in_router(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a1.start()
        handle.finish_attempt(a1, upstream_status=500, raw_usage={
            "prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100,
        })
        a1.close()
        handle.finalize()
        assert handle.router.usage_aggregate.input_tokens == 100
        assert handle.router.fail_count == 1

    def test_retry_waste_can_be_derived(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a1.start()
        handle.finish_attempt(a1, error=TimeoutError("t"), raw_usage={
            "prompt_tokens": 50, "completion_tokens": 0, "total_tokens": 50,
        })
        a1.close()
        a2 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a2.start()
        handle.finish_attempt(a2, upstream_status=200, raw_usage={
            "prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70,
        })
        a2.close()
        handle.finalize()

        router_total = handle.router.usage_aggregate.total_tokens
        final_success = a2.usage.total_tokens
        retry_waste = router_total - final_success
        assert router_total == 120
        assert retry_waste == 50

    def test_cross_process_trace_does_not_require_shared_contextvar(self, tracer):
        """Usage ownership holds with no active gateway context at all (the
        cross-process case: the LLM side simply records its logical usage)."""
        # No runtime/router active in this "process".
        assert router_usage_for_llm() is None

        class FakeLLMSpan:
            def __init__(self):
                self.attributes = {}
            def set_attribute(self, k, v):
                self.attributes[k] = v

        span = FakeLLMSpan()
        assert apply_router_usage_to_span(span) is False
        assert span.attributes == {}


class TestCostResolvedModel:
    def test_cost_uses_resolved_model(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a1.start()
        handle.finish_attempt(a1, upstream_status=200, raw_usage={
            "prompt_tokens": 1_000_000, "completion_tokens": 500_000,
            "total_tokens": 1_500_000,
        })
        a1.close()
        handle.finalize()
        cost = a1.cost
        assert cost is not None
        assert cost.input_cost == pytest.approx(2.0)
        assert cost.output_cost == pytest.approx(4.0)
        assert cost.total_cost == pytest.approx(6.0)
        assert cost.cost_source == "priced"

    def test_cost_unknown_model_is_unpriced(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt({"resolved_model": "no-such-model"})
        a1.start()
        handle.finish_attempt(a1, upstream_status=200, raw_usage={
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        })
        a1.close()
        handle.finalize()
        assert a1.cost is not None
        assert a1.cost.cost_source == "unpriced"
        assert a1.cost.total_cost is None

    def test_cache_explicit_cost_is_preserved(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        explicit = NormalizedCost(
            input_cost=0.1, output_cost=0.2, total_cost=0.3,
            currency="USD", cost_source="priced",
        )
        handle.cache_hit(
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            cost=explicit,
        )
        handle.finalize()
        assert handle.router.cost_aggregate is explicit

    def test_retry_cost_sums_all_attempts(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        for i in range(2):
            a = handle.start_attempt({"resolved_model": "gpt-5.6"})
            a.start()
            handle.finish_attempt(a, error=TimeoutError("t"), raw_usage={
                "prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000,
            })
            a.close()
        a3 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a3.start()
        handle.finish_attempt(a3, upstream_status=200, raw_usage={
            "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "total_tokens": 2_000_000,
        })
        a3.close()
        handle.finalize()
        total = handle.router.cost_aggregate.total_cost
        # 2 failed × $2 + 1 success × ($2 + $8) = $14
        assert total == pytest.approx(14.0)

    def test_failed_attempt_cost_included(self, tracer):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({})
        a1 = handle.start_attempt({"resolved_model": "gpt-5.6"})
        a1.start()
        handle.finish_attempt(a1, upstream_status=500, raw_usage={
            "prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000,
        })
        a1.close()
        handle.finalize()
        assert handle.router.cost_aggregate.total_cost == pytest.approx(2.0)

    def test_pricing_unit_is_per_1m_tokens(self):
        calc = CostCalculator(pricing_table=PRICING)
        usage = NormalizedUsage(input_tokens=1_000_000, output_tokens=1_000_000,
                                total_tokens=2_000_000, usage_source="test")
        cost = calc.calculate(usage, model="gpt-5.6")
        # $2/1M in + $8/1M out for exactly 1M each → $10 total.
        assert cost.total_cost == pytest.approx(10.0)
