"""SDK LLM usage aggregation hook (spec §12.3, langchain-observability delta).

When a Gateway Router exists for an SDK LLM logical call, the LLM span's final
Usage equals the Router aggregate (never just the final successful attempt).
Without a Router, the LLM keeps its own directly-measured Usage.
"""
import logging
from typing import Optional

from .context import GatewayContext

logger = logging.getLogger("llm_obs.gateway.aggregation")


def router_usage_for_llm() -> Optional[dict]:
    """Return the current Router aggregate usage as ``gen_ai.usage.*`` attrs.

    Returns:
        A dict of ``gen_ai.usage.*`` attributes (or ``usage.*``-style keys the
        LLM span expects), or None when no Router is active / no aggregate.
    """
    state = GatewayContext.get()
    router = state.router if state is not None else None
    if router is None:
        return None
    try:
        aggregate = getattr(router, "usage_aggregate", None)
        if aggregate is None:
            return None
        attrs = {}
        for key, value in (
            ("input_tokens", aggregate.input_tokens),
            ("output_tokens", aggregate.output_tokens),
            ("total_tokens", aggregate.total_tokens),
            ("cached_input_tokens", aggregate.cached_input_tokens),
            ("reasoning_tokens", aggregate.reasoning_tokens),
            ("cache_creation_tokens", aggregate.cache_creation_tokens),
            ("cache_read_tokens", aggregate.cache_read_tokens),
        ):
            if value is not None:
                attrs[f"gen_ai.usage.{key}"] = value
        return attrs or None
    except Exception as e:
        logger.error("Router usage for LLM failed: %s", e)
        return None


def apply_router_usage_to_span(span) -> bool:
    """Overwrite an SDK LLM span's usage attrs with the Router aggregate.

    Only applied when a Router is active AND has aggregated usage (i.e. the
    LLM call routed through the gateway). Returns True when applied.
    """
    attrs = router_usage_for_llm()
    if not attrs:
        return False
    try:
        for key, value in attrs.items():
            span.set_attribute(key, value)
        return True
    except Exception as e:
        logger.error("Apply router usage to LLM span failed: %s", e)
        return False
