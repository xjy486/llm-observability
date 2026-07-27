"""P0-1: Async OpenAI Instrumentor tests."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import openai
from llm_observability import Observability
from llm_observability.instrumentation.openai import OpenAIInstrumentor, AsyncObservedStream
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="async-test", endpoint="http://localhost:99999", auto_instrument_openai=False)
    yield Observability._tracer
    Observability.shutdown()


def test_openai_async_instrumentor_patches_async_create(init_sdk):
    """AsyncCompletions.create is patched after instrument()."""
    instr = OpenAIInstrumentor()
    original_async = openai.resources.chat.completions.AsyncCompletions.create
    instr.instrument(tracer=init_sdk)
    assert openai.resources.chat.completions.AsyncCompletions.create is not original_async
    instr.uninstrument()
    assert openai.resources.chat.completions.AsyncCompletions.create is original_async


def test_openai_async_uninstrument_restores_original(init_sdk):
    """uninstrument restores both sync and async."""
    instr = OpenAIInstrumentor()
    orig_sync = openai.resources.chat.completions.Completions.create
    orig_async = openai.resources.chat.completions.AsyncCompletions.create
    instr.instrument(tracer=init_sdk)
    instr.uninstrument()
    assert openai.resources.chat.completions.Completions.create is orig_sync
    assert openai.resources.chat.completions.AsyncCompletions.create is orig_async


def test_openai_async_dedup_still_injects_traceparent(init_sdk):
    """When logical_llm_span_active=True, async create still injects headers, no span."""
    tracer = init_sdk
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    async def run():
        ctx = SpanContext(trace_id="0"*32, span_id="a"*16, parent_span_id=None,
                          span_kind="AGENT", sampled=True, logical_llm_span_active=True)
        token = set_context(ctx)
        try:
            fake_resp = MagicMock()
            with patch.object(instr, "_original_async_create", new_callable=AsyncMock, return_value=fake_resp) as mock_create:
                client = openai.AsyncOpenAI(api_key="fake", base_url="http://localhost:99999")
                await client.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":"hi"}])
                call_kwargs = mock_create.call_args
                assert "extra_headers" in call_kwargs.kwargs
                headers = call_kwargs.kwargs["extra_headers"]
                assert "traceparent" in headers
        finally:
            reset_context(token)

    asyncio.run(run())
    tracer.reporter.report = orig_report
    instr.uninstrument()
    # No new LLM span created (dedup)
    assert len(captured) == 0


def test_openai_async_nonstreaming_span_lifecycle(init_sdk):
    """Async non-streaming creates LLM span, reports it."""
    tracer = init_sdk
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    async def run():
        ctx = SpanContext(trace_id="0"*32, span_id="a"*16, parent_span_id=None,
                          span_kind="AGENT", sampled=True, logical_llm_span_active=False)
        token = set_context(ctx)
        try:
            fake_resp = MagicMock()
            fake_resp.model = "gpt-4o"
            fake_resp.usage = MagicMock(prompt_tokens=5, completion_tokens=3, total_tokens=8)
            with patch.object(instr, "_original_async_create", new_callable=AsyncMock, return_value=fake_resp):
                client = openai.AsyncOpenAI(api_key="fake", base_url="http://localhost:99999")
                resp = await client.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":"hi"}])
                assert resp is fake_resp
        finally:
            reset_context(token)

    asyncio.run(run())
    tracer.reporter.report = orig_report
    instr.uninstrument()

    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    assert len(llm_spans) == 1
    assert llm_spans[0]["status"] == "OK"
    assert llm_spans[0].get("attributes", {}).get("gen_ai.usage.total_tokens") == 8


def test_openai_async_streaming_span_lifecycle(init_sdk):
    """Async streaming wraps in AsyncObservedStream, span ends on exhaustion."""
    tracer = init_sdk
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    async def run():
        ctx = SpanContext(trace_id="0"*32, span_id="a"*16, parent_span_id=None,
                          span_kind="AGENT", sampled=True, logical_llm_span_active=False)
        token = set_context(ctx)
        try:
            chunks = [MagicMock(), MagicMock()]
            for c in chunks:
                c.choices = [MagicMock(delta=MagicMock(content="hi"))]
                c.usage = None

            class FakeAsyncStream:
                def __init__(self, items):
                    self._items = list(items)
                    self._idx = 0
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    if self._idx >= len(self._items):
                        raise StopAsyncIteration
                    item = self._items[self._idx]
                    self._idx += 1
                    return item
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass
                def close(self):
                    pass

            with patch.object(instr, "_original_async_create", new_callable=AsyncMock, return_value=FakeAsyncStream(chunks)):
                client = openai.AsyncOpenAI(api_key="fake", base_url="http://localhost:99999")
                result = await client.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":"hi"}], stream=True)
                assert isinstance(result, AsyncObservedStream)
                collected = []
                async for chunk in result:
                    collected.append(chunk)
                assert len(collected) == 2
        finally:
            reset_context(token)

    asyncio.run(run())
    tracer.reporter.report = orig_report
    instr.uninstrument()

    llm_spans = [r for r in captured if r["span_kind"] == "LLM"]
    assert len(llm_spans) == 1
    assert llm_spans[0]["status"] == "OK"
