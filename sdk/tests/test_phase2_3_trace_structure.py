"""P1-5: Streaming explicit close tests."""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import get_current_context, SpanContext, set_context, reset_context
from llm_observability.integrations.langchain.agent_wrapper import observe_agent


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="stream-test", endpoint="http://localhost:99999")
    yield Observability._tracer
    Observability.shutdown()


def test_stream_explicit_close_finalizes_agent(init_sdk):
    """Sync stream: gen.close() finalizes AGENT span and restores context."""
    tracer = init_sdk
    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    def fake_stream(input, config=None, **kwargs):
        yield "chunk1"
        yield "chunk2"

    fake_agent = MagicMock()
    fake_agent.stream = fake_stream

    observed = observe_agent(fake_agent, name="stream-close-test")
    gen = observed.stream({"messages": []})
    first = next(gen)
    assert first == "chunk1"
    gen.close()

    tracer.reporter.report = orig_report
    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    assert agent_spans[0]["status"] in ("OK", "UNSET")  # not ERROR
    # Context restored
    assert get_current_context() is None


def test_astream_explicit_aclose_finalizes_agent(init_sdk):
    """Async stream: await agen.aclose() finalizes AGENT span."""
    tracer = init_sdk
    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    async def fake_astream(input, config=None, **kwargs):
        yield "chunk1"
        yield "chunk2"

    fake_agent = MagicMock()
    fake_agent.astream = fake_astream

    observed = observe_agent(fake_agent, name="astream-close-test")

    async def run():
        agen = observed.astream({"messages": []})
        first = await anext(agen)
        assert first == "chunk1"
        await agen.aclose()

    asyncio.run(run())

    tracer.reporter.report = orig_report
    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    assert agent_spans[0]["status"] in ("OK", "UNSET")


def test_astream_cancel_restores_context(init_sdk):
    """asyncio.CancelledError during astream restores context."""
    tracer = init_sdk

    async def fake_astream(input, config=None, **kwargs):
        yield "chunk1"
        await asyncio.sleep(100)

    fake_agent = MagicMock()
    fake_agent.astream = fake_astream

    observed = observe_agent(fake_agent, name="cancel-test")

    async def run():
        agen = observed.astream({"messages": []})
        first = await anext(agen)
        raise asyncio.CancelledError()

    try:
        asyncio.run(run())
    except (asyncio.CancelledError, RuntimeError):
        pass

    assert get_current_context() is None
