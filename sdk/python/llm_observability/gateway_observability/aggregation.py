"""SDK LLM usage aggregation hook — REMOVED semantics (rework P0-7).

Previously this module wrote the Gateway Router aggregate back into the SDK
LLM span via a process-local ContextVar ("LLM Usage SHALL equal the Router
aggregate"). That ownership model only holds when SDK and gateway runtime
share one Python process; real deployments run them in separate processes, so
the ContextVar write-back silently produces wrong data.

Frozen ownership (rework):
- Attempt  — the single real upstream request's Usage/Cost.
- Router   — the aggregate of ALL attempts (incl. failed/retried).
- SDK LLM  — the LOGICAL response usage seen by the caller; NOT required to
             equal the Router aggregate. Core/UI derive Retry Waste from the
             trace tree (Router aggregate − final successful attempt).

The functions below are kept as no-op shims so existing imports don't break;
they never write anything. An explicit header-based protocol
(``x-llm-obs-*``) may be designed separately to expose the aggregate.
"""
import logging
from typing import Optional

logger = logging.getLogger("llm_obs.gateway.aggregation")


def router_usage_for_llm() -> Optional[dict]:
    """Deprecated no-op: returns None.

    The SDK LLM span keeps its own logical response usage; the Router
    aggregate is never written back through a ContextVar (P0-7).
    """
    return None


def apply_router_usage_to_span(span) -> bool:
    """Deprecated no-op: never mutates the span, returns False (P0-7)."""
    return False
