"""CostCalculator tests (spec §12.2)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import (
    CostCalculator,
    NormalizedUsage,
    add_cost,
)


def _usage(prompt=10, completion=5):
    return NormalizedUsage(input_tokens=prompt, output_tokens=completion,
                           total_tokens=(prompt + completion))


def test_unpriced_without_table():
    calc = CostCalculator()  # no pricing table
    cost = calc.calculate(_usage())
    assert cost is not None
    assert cost.cost_source == "unpriced"
    assert cost.total_cost is None
    assert cost.currency == "USD"


def test_priced_with_table():
    calc = CostCalculator({"gpt-5.6": {"input": 0.001, "output": 0.002}})
    u = _usage(1000, 500)
    cost = calc.calculate(u, model="gpt-5.6")
    assert cost.cost_source == "priced"
    assert cost.input_cost == 1.0
    assert cost.output_cost == 1.0
    assert cost.total_cost == 2.0


def test_calc_failure_fail_open():
    class BadUsage:
        input_tokens = "not-an-int"
        output_tokens = "boom"
    calc = CostCalculator()
    # No exception propagates
    cost = calc.calculate(BadUsage(), model="m")  # type: ignore
    assert cost is None or cost.cost_source == "unpriced"


def test_add_cost_aggregation():
    a = CostCalculator({"m": {"input": 0.001, "output": 0.002}}).calculate(_usage(1000, 500), model="m")
    b = CostCalculator({"m": {"input": 0.001, "output": 0.002}}).calculate(_usage(2000, 1000), model="m")
    agg = add_cost(a, b)
    assert agg.input_cost == 3.0
    assert agg.output_cost == 3.0
    assert agg.total_cost == 6.0


def test_add_cost_none_safe():
    a = CostCalculator({"m": {"input": 0.001, "output": 0.002}}).calculate(_usage(1000, 500), model="m")
    agg = add_cost(a, None)
    assert agg.input_cost == 1.0
    assert agg.cost_source == "priced"


def test_cost_to_attributes_keys():
    from llm_observability.gateway_observability.cost import cost_to_attributes
    cost = CostCalculator().calculate(_usage())
    attrs = cost_to_attributes(cost)
    assert attrs["cost.source"] == "unpriced"
    assert attrs["cost.currency"] == "USD"
    assert "cost.total" not in attrs  # None → not recorded
