"""Sampling inheritance and propagation (spec §17, runtime spec).

When a legitimate upstream ``traceparent`` exists its ``trace_flags`` decide
sampling (01 → sample, 00 → don't report) and are never overridden by gateway
re-sampling. With no upstream trace the runtime creates a Root Router per the
local ``sample_rate``.
"""
import logging
from typing import Optional

from ..propagation import extract_traceparent

logger = logging.getLogger("llm_obs.gateway.propagation")

# Marker attribute set on a locally-sampled Root Router (no upstream trace).
ROOT_ROUTER_SAMPLED_ATTR = "gateway.root_router_sampled"

TRACE_FLAG_SAMPLED = "01"
TRACE_FLAG_NOT_SAMPLED = "00"


def sampling_from_traceparent(header_value: Optional[str]) -> Optional[bool]:
    """Decide sampling from an upstream traceparent header.

    Returns:
        True when trace_flags=01 (sample), False when trace_flags=00
        (do not report), or None when no valid traceparent exists.
    """
    if not header_value:
        return None
    extracted = extract_traceparent(str(header_value).strip())
    if extracted is None:
        return None
    return extracted.trace_flags == TRACE_FLAG_SAMPLED


def extract_traceparent_ids(header_value: Optional[str]):
    """Extract (trace_id, parent_span_id, sampled) from an upstream traceparent.

    Returns (None, None, None) when no valid traceparent exists. Used so the
    Router span inherits the upstream trace_id and still propagates the same
    trace context downstream (spec §17.3).
    """
    if not header_value:
        return None, None, None
    extracted = extract_traceparent(str(header_value).strip())
    if extracted is None:
        return None, None, None
    return (
        extracted.trace_id,
        extracted.parent_span_id,
        extracted.trace_flags == TRACE_FLAG_SAMPLED,
    )


def decide_sampling(header_value: Optional[str], local_sample_rate: float) -> bool:
    """Decide whether this gateway trace reports to the backend.

    Honors an upstream sampling decision when present; otherwise applies the
    local sample rate. Never re-randomizes an upstream decision.
    """
    inherited = sampling_from_traceparent(header_value)
    if inherited is not None:
        return inherited
    import random
    return random.random() < local_sample_rate


def inject_downstream_trace_headers(router, attempt=None) -> dict:
    """Build the W3C ``traceparent`` header for an Attempt's upstream request.

    The downstream traceparent carries the Router's trace ID, the Attempt's
    span ID as parent (falling back to the Router span when no attempt), and
    the inherited sampling decision (``00`` when sampled out — still
    propagated, never re-randomized).

    Returns ``{}`` when no span exists (fail-open).
    """
    try:
        router_span = router.span if router is not None else None
        if router_span is None or not router_span.trace_id:
            return {}
        parent_span_id = router_span.span_id
        if attempt is not None:
            attempt_span = getattr(attempt, "span", None)
            if attempt_span is not None and attempt_span.span_id:
                parent_span_id = attempt_span.span_id
        sampled = bool(getattr(router, "_sampled", True))
        flags = TRACE_FLAG_SAMPLED if sampled else TRACE_FLAG_NOT_SAMPLED
        return {
            "traceparent": f"00-{router_span.trace_id}-{parent_span_id}-{flags}",
        }
    except Exception as e:
        logger.error("Gateway downstream header injection failed: %s", e)
        return {}
