"""Tests for SpanContext and ContextVar propagation."""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from llm_observability.context import (
    SpanContext,
    get_current_context,
    set_context,
    reset_context,
)
from llm_observability.utils.ids import generate_trace_id, generate_span_id


def test_context_is_none_by_default():
    """No active context when nothing is set."""
    assert get_current_context() is None


def test_set_and_get_context():
    """Set a context and retrieve it."""
    ctx = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=None,
        span_kind="AGENT",
        sampled=True,
    )
    token = set_context(ctx)
    assert get_current_context() is ctx
    reset_context(token)
    assert get_current_context() is None


def test_context_isolation_between_calls():
    """ContextVar ensures each call scope is isolated."""
    ctx1 = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=None,
        span_kind="AGENT",
        sampled=True,
    )
    token = set_context(ctx1)

    # Simulate a nested call that sets its own context
    ctx2 = SpanContext(
        trace_id=ctx1.trace_id,
        span_id=generate_span_id(),
        parent_span_id=ctx1.span_id,
        span_kind="LLM",
        sampled=True,
    )
    token2 = set_context(ctx2)
    assert get_current_context().span_id == ctx2.span_id
    assert get_current_context().parent_span_id == ctx1.span_id

    reset_context(token2)
    assert get_current_context().span_id == ctx1.span_id

    reset_context(token)
    assert get_current_context() is None


def test_async_context_isolation():
    """Concurrent asyncio tasks must not share context."""
    results = {}

    async def task(name: str, kind: str):
        ctx = SpanContext(
            trace_id=generate_trace_id(),
            span_id=generate_span_id(),
            parent_span_id=None,
            span_kind=kind,
            sampled=True,
        )
        token = set_context(ctx)
        await asyncio.sleep(0.01)
        current = get_current_context()
        results[name] = current.span_id
        reset_context(token)

    async def main():
        await asyncio.gather(
            task("a", "AGENT"),
            task("b", "LLM"),
        )

    asyncio.run(main())
    assert results["a"] != results["b"]


def test_logical_llm_span_active_flag():
    """The logical_llm_span_active flag defaults to False."""
    ctx = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=None,
        span_kind="AGENT",
        sampled=True,
    )
    assert ctx.logical_llm_span_active is False

    ctx.logical_llm_span_active = True
    assert ctx.logical_llm_span_active is True
