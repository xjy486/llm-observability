"""P0-4: streaming terminal-state finalization tests (adversarial).

Covers:
- Success/error/cancel aggregate to the Router exactly once with consistent
  Router/Attempt terminal states (never one OK, one ERROR).
- No success is aggregated at header/wrapper-creation time.
- TTFT ignores keepalives/empty/metadata-only/usage-only/[DONE] chunks and
  is measured from the real upstream request start.
- close()/aclose() are idempotent.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    ErrorCategory,
    GatewayStream,
    AsyncGatewayStream,
)
from llm_observability.gateway_observability.attributes import ATTR_ATTEMPT, ATTR_ROUTER
from llm_observability.gateway_observability.context import GatewayContext, clear_gateway_context
from llm_observability.gateway_observability.streaming import is_meaningful_content


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


def _make_stream_handle(tracer, chunks):
    runtime = GatewayRuntime(tracer=tracer, sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    handle = runtime.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    attempt = handle.start_attempt()
    attempt.start()
    return handle, handle.router, attempt, chunks


class TestStreamTerminalConsistency:
    def test_stream_success_aggregates_attempt_once(self, tracer):
        handle, router, attempt, chunks = _make_stream_handle(tracer, iter(["hello", "world"]))
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        list(stream)
        assert router.success_count == 1
        assert router.fail_count == 0
        assert router.span.status == "OK"
        assert attempt.span.status == "OK"

    def test_stream_cancel_router_and_attempt_both_error(self, tracer):
        handle, router, attempt, chunks = _make_stream_handle(tracer, iter(["a", "b", "c"]))
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        next(iter(stream))
        stream.close()
        assert attempt.span.status == "ERROR"
        assert attempt.span.attributes[ATTR_ATTEMPT["error_category"]] == ErrorCategory.CLIENT_CANCELLED
        assert router.span.status == "ERROR", "Router must not be OK when the attempt errored"
        assert router.span.attributes[ATTR_ROUTER["final_error_category"]] == ErrorCategory.CLIENT_CANCELLED

    def test_stream_error_router_and_attempt_both_error(self, tracer):
        def exploding():
            yield "a"
            raise ConnectionError("upstream reset")
        handle, router, attempt, chunks = _make_stream_handle(tracer, exploding())
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        with pytest.raises(ConnectionError):
            list(stream)
        assert attempt.span.status == "ERROR"
        assert router.span.status == "ERROR"
        assert (
            router.span.attributes[ATTR_ROUTER["final_error_category"]]
            == attempt.span.attributes[ATTR_ATTEMPT["error_category"]]
        )

    def test_stream_timeout_router_final_error_timeout(self, tracer):
        def timing_out():
            yield "a"
            raise TimeoutError("read timed out")
        handle, router, attempt, chunks = _make_stream_handle(tracer, timing_out())
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        with pytest.raises(TimeoutError):
            list(stream)
        assert attempt.span.attributes[ATTR_ATTEMPT["error_category"]] == ErrorCategory.TIMEOUT
        assert router.span.attributes[ATTR_ROUTER["final_error_category"]] == ErrorCategory.TIMEOUT
        assert router.span.status == "ERROR"

    def test_stream_does_not_aggregate_success_before_terminal_state(self, tracer):
        handle, router, attempt, chunks = _make_stream_handle(tracer, iter(["a", "b"]))
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        # Wrapper created ("headers received") — nothing aggregated, spans open.
        # (Span.end_time == 0 is the codebase "not ended" sentinel.)
        assert router.success_count == 0
        assert router.attempts[0].span.end_time == 0
        assert router.span.end_time == 0
        it = iter(stream)
        next(it)  # mid-stream — still nothing aggregated
        assert router.success_count == 0
        assert router.span.end_time == 0
        list(it)  # finish
        assert router.success_count == 1

    def test_stream_attempt_result_registered_once(self, tracer):
        handle, router, attempt, chunks = _make_stream_handle(tracer, iter(["a", "[DONE]"]))
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        list(stream)
        stream.close()  # late close after [DONE] — must not re-aggregate
        assert router.success_count == 1
        assert router.fail_count == 0

    def test_stream_usage_aggregated_at_terminal_chunk(self, tracer):
        chunks = iter([
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}},
        ])
        handle, router, attempt, chunks = _make_stream_handle(tracer, chunks)
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        list(stream)
        assert router.usage_aggregate is not None
        assert router.usage_aggregate.input_tokens == 5
        assert router.usage_aggregate.output_tokens == 7
        assert attempt.span.attributes["usage.input_tokens"] == 5

    def test_stream_partial_usage_recorded_on_cancel(self, tracer):
        chunks = iter([
            {"choices": [{"delta": {"content": "hi"}}]},
            {"usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
            {"choices": [{"delta": {"content": "more"}}]},
        ])
        handle, router, attempt, chunks = _make_stream_handle(tracer, chunks)
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        it = iter(stream)
        next(it)
        next(it)
        stream.close()
        assert attempt.span.attributes.get("usage.input_tokens") == 3
        assert router.usage_aggregate is not None
        assert router.usage_aggregate.input_tokens == 3


class TestStreamTTFT:
    def test_stream_ttft_ignores_keepalive(self, tracer):
        chunks = iter([": keepalive", "", "real content"])
        handle, router, attempt, chunks = _make_stream_handle(tracer, chunks)
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        it = iter(stream)
        next(it)  # keepalive — no TTFT
        next(it)  # empty — no TTFT
        assert router.ttft_ms is None
        next(it)  # real content — TTFT now
        assert router.ttft_ms is not None
        list(it)

    def test_stream_ttft_ignores_done_marker(self, tracer):
        handle, router, attempt, chunks = _make_stream_handle(tracer, iter(["[DONE]"]))
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        list(stream)
        assert router.ttft_ms is None, "[DONE] must never trigger TTFT"

    def test_stream_ttft_ignores_metadata_and_usage_only_chunks(self, tracer):
        chunks = iter([
            {"id": "chatcmpl-1", "choices": []},                     # metadata only
            {"choices": [], "usage": {"prompt_tokens": 2}},          # usage only
            {"choices": [{"delta": {"content": "x"}}]},              # content
        ])
        handle, router, attempt, chunks = _make_stream_handle(tracer, chunks)
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        it = iter(stream)
        next(it)
        next(it)
        assert router.ttft_ms is None
        next(it)
        assert router.ttft_ms is not None
        list(it)

    def test_stream_ttft_measured_from_request_start_not_wrapper_creation(self, tracer):
        handle, router, attempt, chunks = _make_stream_handle(tracer, iter(["content"]))
        # Simulate wrapper created long after the upstream request started.
        attempt._started_at = time.time() - 1.5
        time.sleep(0.05)
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        list(stream)
        assert router.ttft_ms is not None
        assert router.ttft_ms >= 1500, "TTFT must start from the real upstream request start"


class TestStreamCloseIdempotent:
    def test_stream_close_is_idempotent(self, tracer):
        handle, router, attempt, chunks = _make_stream_handle(tracer, iter(["a", "b"]))
        stream = GatewayStream(chunks, router, attempt, runtime_handle=handle)
        stream.close()
        stream.close()
        stream.close()
        assert router.fail_count == 1  # exactly one cancelled aggregation
        assert router.success_count == 0

    def test_async_stream_cancelled_error_finalizes_once(self, tracer):
        async def run():
            async def agen():
                # Block on the first __anext__ so the consumer's await is
                # genuinely in-flight when cancelled (cancel lands mid-stream).
                await asyncio.sleep(10)
                yield "a"
                yield "b"
            handle, router, attempt, _ = _make_stream_handle(tracer, None)
            stream = AsyncGatewayStream(agen(), router, attempt, runtime_handle=handle)
            task = asyncio.ensure_future(stream.__anext__())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await stream.aclose()  # second terminal path — no-op
            return router, attempt
        router, attempt = asyncio.run(run())
        assert router.fail_count == 1
        assert attempt.span.attributes[ATTR_ATTEMPT["error_category"]] == ErrorCategory.CLIENT_CANCELLED
        assert router.span.attributes[ATTR_ROUTER["final_error_category"]] == ErrorCategory.CLIENT_CANCELLED

    def test_async_stream_aclose_finalizes_once(self, tracer):
        async def run():
            async def agen():
                yield "a"
                yield "b"
            handle, router, attempt, _ = _make_stream_handle(tracer, None)
            stream = AsyncGatewayStream(agen(), router, attempt, runtime_handle=handle)
            await stream.aclose()
            await stream.aclose()
            return router
        router = asyncio.run(run())
        assert router.fail_count == 1
        assert router.success_count == 0


class TestMeaningfulContentPredicate:
    @pytest.mark.parametrize("chunk", [
        None, "", "   ", "[DONE]", ": keepalive", ":ka\n",
        "data: {...}", "event: message",
        {"usage": {"prompt_tokens": 1}},
        {"choices": []},
        {"choices": [{"delta": {}}]},
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"type": "message_start"},
        {"type": "ping"},
    ])
    def test_non_content_chunks_are_not_meaningful(self, chunk):
        assert is_meaningful_content(chunk) is False

    @pytest.mark.parametrize("chunk", [
        "hello",
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"delta": {"tool_calls": [{"id": "1"}]}}]},
        {"choices": [{"message": {"content": "hi"}}]},
        {"type": "content_block_delta"},
    ])
    def test_content_chunks_are_meaningful(self, chunk):
        assert is_meaningful_content(chunk) is True
