"""P1: Tool fail-open context leak — reset_context must be in finally.

Tests that TOOL context is restored even when __exit__ steps throw,
e.g. str(exc_val) raises during error recording.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context
from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="ctx-leak-test", endpoint="http://localhost:99999")
    ctx = SpanContext(
        trace_id="0" * 32, span_id="a" * 16,
        parent_span_id=None, span_kind="AGENT", sampled=True,
    )
    token = set_context(ctx)
    yield Observability._tracer
    reset_context(token)
    Observability.shutdown()


def test_tool_context_restored_when_str_exception_fails(init_sdk):
    """If str(exc_val) raises during error recording, context is still restored."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    request.tool_call = {"name": "bad_tool", "args": {}, "id": "tc1"}

    class BadStrException(Exception):
        def __str__(self):
            raise RuntimeError("str() failed")

    handler = MagicMock(side_effect=BadStrException())
    parent_ctx = get_current_context()

    with pytest.raises(BadStrException):
        mw.wrap_tool_call(request, handler)

    # Context MUST be restored to parent even though str() failed in __exit__
    assert get_current_context() is parent_ctx, (
        "TOOL context leaked — reset_context must be in finally block"
    )


def test_tool_context_restored_when_reporter_fails(init_sdk):
    """If reporter.report raises, context is still restored."""
    tracer = init_sdk
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    request.tool_call = {"name": "search", "args": {}, "id": "tc1"}
    handler = MagicMock(return_value="ok")
    parent_ctx = get_current_context()

    import unittest.mock as mock
    with mock.patch.object(tracer.reporter, "report", side_effect=RuntimeError("reporter dead")):
        result = mw.wrap_tool_call(request, handler)

    assert result == "ok"
    assert get_current_context() is parent_ctx, "TOOL context leaked when reporter failed"


def test_tool_context_restored_when_span_end_fails(init_sdk):
    """If span.end() raises, context is still restored."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    request.tool_call = {"name": "search", "args": {}, "id": "tc1"}
    handler = MagicMock(return_value="ok")
    parent_ctx = get_current_context()

    import unittest.mock as mock
    with mock.patch("llm_observability.spans.Span.end", side_effect=RuntimeError("end failed")):
        result = mw.wrap_tool_call(request, handler)

    assert result == "ok"
    assert get_current_context() is parent_ctx, "TOOL context leaked when span.end() failed"
