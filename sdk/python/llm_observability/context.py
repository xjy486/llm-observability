"""Span context management using contextvars.

Uses ContextVar (not thread-local) for correct async/await propagation.
"""
from dataclasses import dataclass
from typing import Optional
from contextvars import ContextVar, Token


@dataclass
class SpanContext:
    """Active span context propagated via ContextVar.

    Attributes:
        trace_id: W3C trace ID (32 hex chars).
        span_id: Current span ID (16 hex chars).
        parent_span_id: Parent span ID, None for root.
        span_kind: SpanKind constant.
        sampled: Whether this trace is sampled.
        logical_llm_span_active: True when a logical LLM span is active
            (used for span ownership / dedup — see spec §7, §19).
    """
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    span_kind: str
    sampled: bool = True
    logical_llm_span_active: bool = False


_context_var: ContextVar[Optional[SpanContext]] = ContextVar(
    "llm_obs_context", default=None
)


def get_current_context() -> Optional[SpanContext]:
    """Get the currently active SpanContext, or None."""
    return _context_var.get()


def set_context(ctx: SpanContext) -> Token:
    """Set the current SpanContext. Returns a token for reset."""
    return _context_var.set(ctx)


def reset_context(token: Token):
    """Reset context to its previous value using the token."""
    _context_var.reset(token)
