"""Normalized cost data model and fail-open calculator (spec §8.5, §12.2).

CostCalculator maps a NormalizedUsage onto a NormalizedCost. With no pricing
table it emits ``cost.source = "unpriced"`` and ``total_cost = None`` rather
than failing. Calc failures are fail-open.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from .usage import NormalizedUsage

logger = logging.getLogger("llm_obs.gateway.cost")


@dataclass(frozen=True)
class NormalizedCost:
    """Provider-neutral monetary cost (spec §8.5).

    Attributes:
        input_cost: Cost of input tokens (USD).
        output_cost: Cost of output tokens (USD).
        total_cost: Total cost; None when unpriced.
        currency: Currency code (default USD).
        cost_source: 'unpriced' when no pricing table; else a table source name.
    """
    input_cost: Optional[float] = None
    output_cost: Optional[float] = None
    total_cost: Optional[float] = None
    currency: str = "USD"
    cost_source: Optional[str] = None


def add_cost(a: Optional[NormalizedCost], b: Optional[NormalizedCost]) -> NormalizedCost:
    """Add two normalized costs field-by-field (None-safe).

    When any input is priced, the aggregate is priced; otherwise unpriced.
    """
    def _sum(x, y):
        if x is None:
            return y
        if y is None:
            return x
        return x + y

    priced = False
    for c in (a, b):
        if c is not None and c.cost_source is not None and c.cost_source != "unpriced":
            priced = True
    if not priced and (a is not None and b is not None) and (a.total_cost is not None or b.total_cost is not None):
        priced = True
    return NormalizedCost(
        input_cost=_sum(a.input_cost if a else None, b.input_cost if b else None),
        output_cost=_sum(a.output_cost if a else None, b.output_cost if b else None),
        total_cost=_sum(a.total_cost if a else None, b.total_cost if b else None),
        currency=(a.currency if a else None) or (b.currency if b else None) or "USD",
        cost_source="priced" if priced else "unpriced",
    )


def cost_to_attributes(cost: Optional[NormalizedCost]) -> dict:
    """Map a NormalizedCost onto the fixed ``cost.*`` attribute names."""
    if cost is None:
        return {}
    from .attributes import ATTR_COST
    result = {}
    if cost.input_cost is not None:
        result[ATTR_COST["input"]] = cost.input_cost
    if cost.output_cost is not None:
        result[ATTR_COST["output"]] = cost.output_cost
    if cost.total_cost is not None:
        result[ATTR_COST["total"]] = cost.total_cost
    result[ATTR_COST["currency"]] = cost.currency
    if cost.cost_source is not None:
        result[ATTR_COST["source"]] = cost.cost_source
    return result


class CostCalculator:
    """Map NormalizedUsage → NormalizedCost (fail-open).

    Pricing-table entries use explicit per-1M-token USD units under the frozen
    keys ``input_usd_per_1m_tokens`` / ``output_usd_per_1m_tokens`` (legacy
    ``input``/``output`` per-token keys are still accepted for
    backward compatibility). With no table entry the cost is
    ``source = "unpriced"`` and ``total_cost = None``.
    """

    def __init__(self, pricing_table: Optional[dict] = None):
        """Args: pricing_table — optional
        ``{model: {"input_usd_per_1m_tokens": x, "output_usd_per_1m_tokens": y}}``.
        """
        self._pricing_table = pricing_table or {}

    def calculate(self, usage: Optional[NormalizedUsage], model: Optional[str] = None) -> Optional[NormalizedCost]:
        """Calculate cost for a normalized usage. Fail-open.

        Args:
            usage: Normalized usage tokens.
            model: Resolved model name used to look up the pricing table.

        Without a pricing-table entry, returns an unpriced NormalizedCost
        (``total_cost = None``, ``cost_source = "unpriced"``).
        """
        try:
            return self._calculate_inner(usage, model)
        except Exception as e:
            logger.error("Cost calculation failed: %s", e)
            return None

    def _calculate_inner(self, usage: Optional[NormalizedUsage], model: Optional[str] = None) -> Optional[NormalizedCost]:
        if usage is None:
            return None

        model = model or getattr(usage, "model", None) or "unknown"
        prices = self._pricing_table.get(model)
        if not prices:
            return NormalizedCost(currency="USD", cost_source="unpriced")

        # Frozen unit: USD per 1M tokens. Legacy per-token keys accepted.
        if "input_usd_per_1m_tokens" in prices or "output_usd_per_1m_tokens" in prices:
            input_per_token = (prices.get("input_usd_per_1m_tokens") or 0) / 1_000_000 or None
            output_per_token = (prices.get("output_usd_per_1m_tokens") or 0) / 1_000_000 or None
            if "input_usd_per_1m_tokens" not in prices:
                input_per_token = None
            if "output_usd_per_1m_tokens" not in prices:
                output_per_token = None
        else:
            input_per_token = prices.get("input")
            output_per_token = prices.get("output")

        input_cost = None
        output_cost = None
        if input_per_token is not None and usage.input_tokens is not None:
            input_cost = round(input_per_token * usage.input_tokens, 6)
        if output_per_token is not None and usage.output_tokens is not None:
            output_cost = round(output_per_token * usage.output_tokens, 6)
        total_cost = None
        if input_cost is not None and output_cost is not None:
            total_cost = round(input_cost + output_cost, 6)
        return NormalizedCost(
            input_cost=input_cost,
            output_cost=output_cost,
            total_cost=total_cost,
            currency="USD",
            cost_source="priced" if (input_cost is not None or output_cost is not None) else "unpriced",
        )
