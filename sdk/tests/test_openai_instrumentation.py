"""Tests for OpenAI instrumentation."""
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from llm_observability import Observability
from llm_observability.context import get_current_context, set_context, reset_context, SpanContext
from llm_observability.instrumentation.openai import OpenAIInstrumentor
from llm_observability.utils.ids import generate_trace_id, generate_span_id

try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

import pytest

pytestmark = pytest.mark.skipif(not HAS_OPENAI, reason="openai not installed")


def _setup_sdk():
    """Initialize SDK without auto-instrumentation, return tracer."""
    Observability.init(
        app_name="test-app",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )
    return Observability._tracer


def _teardown():
    Observability.shutdown()


def _make_fake_response():
    """Create a fake OpenAI ChatCompletion response object."""
    fake_response = MagicMock()
    fake_response.model = "gpt-4"
    fake_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    fake_response.model_dump.return_value = {
        "id": "chatcmpl-123",
        "model": "gpt-4",
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    return fake_response


def test_instrumentor_patches_openai():
    """instrument() patches chat.completions.create."""
    original = openai.resources.chat.completions.Completions.create
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=_setup_sdk())
    assert openai.resources.chat.completions.Completions.create is not original
    instr.uninstrument()
    assert openai.resources.chat.completions.Completions.create is original
    _teardown()


def test_llm_span_created_on_openai_call():
    """A call to chat.completions.create creates an LLM span."""
    tracer = _setup_sdk()
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()

    # Patch the module-level _original_create so the patched wrapper calls our mock
    with patch("llm_observability.instrumentation.openai._original_create") as mock_orig:
        mock_orig.return_value = fake_response
        with tracer.trace(name="test-task"):
            client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
            client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": "hello"}],
            )

    # Should have AGENT + LLM spans in queue
    records = list(tracer.reporter._queue)
    kinds = [r["span_kind"] for r in records]
    assert "AGENT" in kinds
    assert "LLM" in kinds

    llm_record = [r for r in records if r["span_kind"] == "LLM"][0]
    assert llm_record["span_name"] == "llm.completion"
    assert llm_record["attributes"].get("gen_ai.request.model") == "gpt-4"

    instr.uninstrument()
    _teardown()


def test_llm_span_parent_is_agent():
    """LLM span parent_span_id must be the AGENT span_id."""
    tracer = _setup_sdk()
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()

    with patch("llm_observability.instrumentation.openai._original_create") as mock_orig:
        mock_orig.return_value = fake_response
        with tracer.trace(name="parent-test"):
            ctx_before = get_current_context()
            agent_span_id = ctx_before.span_id

            client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
            client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "hi"}])

    records = list(tracer.reporter._queue)
    llm_record = [r for r in records if r["span_kind"] == "LLM"][0]
    assert llm_record["parent_span_id"] == agent_span_id

    instr.uninstrument()
    _teardown()


def test_llm_span_error_on_exception():
    """OpenAI exception sets LLM span to ERROR and re-raises."""
    tracer = _setup_sdk()
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    raised = False
    try:
        with patch("llm_observability.instrumentation.openai._original_create") as mock_orig:
            mock_orig.side_effect = RuntimeError("API error")
            with tracer.trace(name="error-task"):
                client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
                client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "hi"}])
    except RuntimeError:
        raised = True

    assert raised
    records = list(tracer.reporter._queue)
    llm_record = [r for r in records if r["span_kind"] == "LLM"][0]
    assert llm_record["status"] == "ERROR"
    assert llm_record["error_type"] == "RuntimeError"

    instr.uninstrument()
    _teardown()


def test_dedup_skips_llm_span_when_active():
    """When logical_llm_span_active is True, no new LLM span is created."""
    tracer = _setup_sdk()
    instr = OpenAIInstrumentor()
    instr.instrument(tracer=tracer)

    fake_response = _make_fake_response()

    # Set a context with logical_llm_span_active=True
    ctx = SpanContext(
        trace_id=generate_trace_id(),
        span_id=generate_span_id(),
        parent_span_id=None,
        span_kind="LLM",
        sampled=True,
        logical_llm_span_active=True,
    )
    token = set_context(ctx)

    queue_before = len(tracer.reporter._queue)

    with patch("llm_observability.instrumentation.openai._original_create") as mock_orig:
        mock_orig.return_value = fake_response
        client = openai.OpenAI(api_key="fake", base_url="http://localhost:99999")
        client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": "hi"}])

    queue_after = len(tracer.reporter._queue)
    assert queue_after == queue_before  # no new span

    reset_context(token)
    instr.uninstrument()
    _teardown()
