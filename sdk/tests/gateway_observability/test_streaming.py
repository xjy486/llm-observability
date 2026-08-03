"""Streaming lifecycle wrapper tests (spec §15, task 4.4).

Covers close()/aclose()/break/CancelledError without residual registry or
ContextVar state.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    ErrorCategory,
    GatewayStream,
    AsyncGatewayStream,
)
from llm_observability.gateway_observability.context import GatewayContext
from llm_observability.gateway_observability.events import (
    EVENT_STREAM_COMPLETED,
    EVENT_STREAM_CANCELLED,
    EVENT_STREAM_FIRST_TOKEN,
)


def _make_router(attempt_registry=None):
    rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    return rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"}), rt


def test_stream_full_consumption_ends_both_spans_ok(clean_sdk):
    handle, _ = _make_router()
    router = handle.router
    attempt = handle.start_attempt({"attempt_index": 1})
    attempt.start()
    handle.finish_attempt(attempt, upstream_status=200)

    chunks = iter(["a", "b", "c"])
    stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
    consumed = list(stream)

    assert consumed == ["a", "b", "c"]
    assert router.span.end_time > 0  # Router ended at terminal state
    assert attempt.span.end_time > 0
    assert [e for e in router.span.events if e["name"] == EVENT_STREAM_COMPLETED]
    assert [e for e in router.span.events if e["name"] == EVENT_STREAM_FIRST_TOKEN]
    assert router.span.attributes["gateway.ttft_ms"] is not None
    # Registry/context clean
    state = GatewayContext.get()
    assert state.router is None and state.active_attempt is None


def test_stream_early_break_finalizes_cancelled(clean_sdk):
    handle, _ = _make_router()
    router = handle.router
    attempt = handle.start_attempt({"attempt_index": 1})
    attempt.start()
    handle.finish_attempt(attempt, upstream_status=200)

    def gen():
        yield "a"
        yield "b"

    stream = GatewayStream(gen(), router, attempt, runtime_handle=handle)
    it = iter(stream)
    next(it)  # first chunk
    stream.close()  # client close before completion

    assert [e for e in router.span.events if e["name"] == EVENT_STREAM_CANCELLED]
    assert attempt.span.attributes["gateway.error_category"] == ErrorCategory.CLIENT_CANCELLED
    assert router.span.end_time > 0
    state = GatewayContext.get()
    assert state.router is None and state.active_attempt is None


def test_stream_upstream_done_marker(clean_sdk):
    handle, _ = _make_router()
    router = handle.router
    attempt = handle.start_attempt({"attempt_index": 1})
    attempt.start()
    handle.finish_attempt(attempt, upstream_status=200)

    chunks = iter(["a", "[DONE]"])
    stream = GatewayStream(chunks, router, attempt, runtime_handle=handle, check_done=True)
    out = []
    try:
        while True:
            out.append(next(stream))
    except StopIteration:
        pass
    assert "[DONE]" in out
    assert [e for e in router.span.events if e["name"] == EVENT_STREAM_COMPLETED]
    assert router.span.end_time > 0


def test_stream_generator_close_on_generator_exit(clean_sdk):
    handle, _ = _make_router()
    router = handle.router
    attempt = handle.start_attempt({"attempt_index": 1})
    attempt.start()
    handle.finish_attempt(attempt, upstream_status=200)

    def gen():
        yield "a"
        yield "b"
        yield "c"

    # with-block + early break → __exit__(None,None,None) → close() → cancelled.
    with GatewayStream(gen(), router, attempt, runtime_handle=handle) as stream:
        for chunk in stream:
            break

    assert [e for e in router.span.events if e["name"] == EVENT_STREAM_CANCELLED]
    assert router.span.end_time > 0
    # Registry/context clean
    state = GatewayContext.get()
    assert state.router is None and state.active_attempt is None


# ── async ──

async def _async_iter(items):
    for item in items:
        yield item
        await asyncio.sleep(0)


def test_async_stream_full_consumption(clean_sdk):
    """Everything runs in ONE asyncio task context (matches aiohttp handler).

    The Router/Attempt ContextVars are set and reset in the same context, so
    after full consumption no stale state remains.
    """
    async def scenario():
        rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
        handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
        router = handle.router
        attempt = handle.start_attempt({"attempt_index": 1})
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200)

        stream = AsyncGatewayStream(_async_iter(["x", "y"]), router, attempt, runtime_handle=handle)
        chunks = []
        async for c in stream:
            chunks.append(c)

        assert chunks == ["x", "y"]
        assert [e for e in router.span.events if e["name"] == EVENT_STREAM_COMPLETED]
        assert router.span.end_time > 0
        # No residual ContextVar in this context.
        state = GatewayContext.get()
        assert state.router is None and state.active_attempt is None
        return router

    router = asyncio.run(scenario())
    assert router.span.attributes["gateway.ttft_ms"] is not None


def test_async_stream_aclose_cancelled(clean_sdk):
    async def scenario():
        rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
        handle = rt.handle_request({"gateway_name": "mock"})
        router = handle.router
        attempt = handle.start_attempt({"attempt_index": 1})
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200)

        stream = AsyncGatewayStream(_async_iter(["x", "y"]), router, attempt, runtime_handle=handle)
        agen = stream.__aiter__()
        await agen.__anext__()
        await stream.aclose()

        assert [e for e in router.span.events if e["name"] == EVENT_STREAM_CANCELLED]
        assert attempt.span.attributes["gateway.error_category"] == ErrorCategory.CLIENT_CANCELLED
        assert router.span.end_time > 0
        state = GatewayContext.get()
        assert state.router is None and state.active_attempt is None

    asyncio.run(scenario())


def test_async_stream_cancelled_error(clean_sdk):
    async def scenario():
        rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
        handle = rt.handle_request({"gateway_name": "mock"})
        router = handle.router
        attempt = handle.start_attempt({"attempt_index": 1})
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200)

        async def cancelling_iter():
            yield "a"
            raise asyncio.CancelledError()

        stream = AsyncGatewayStream(cancelling_iter(), router, attempt, runtime_handle=handle)
        with pytest.raises(asyncio.CancelledError):
            async for _ in stream:
                pass

        assert [e for e in router.span.events if e["name"] == EVENT_STREAM_CANCELLED]
        assert attempt.span.attributes["gateway.error_category"] == ErrorCategory.CLIENT_CANCELLED
        assert router.span.end_time > 0
        state = GatewayContext.get()
        assert state.router is None and state.active_attempt is None

    asyncio.run(scenario())


def test_stream_spans_not_ended_before_consumption(clean_sdk):
    """StreamingResponse return does NOT end spans (spec §15.3)."""
    handle, _ = _make_router()
    router = handle.router
    attempt = handle.start_attempt({"attempt_index": 1})
    attempt.start()
    handle.finish_attempt(attempt, upstream_status=200)

    def gen():
        yield "a"
        yield "b"

    stream = GatewayStream(gen(), router, attempt, runtime_handle=handle)
    # Creating the wrapper does not finalize — spans still open.
    assert router.span.end_time == 0
    assert attempt.span.end_time == 0
    # Even after pulling the first chunk.
    next(iter(stream))
    assert router.span.end_time == 0
    assert attempt.span.end_time == 0
    # close() ends them.
    stream.close()
    assert router.span.end_time > 0
