"""Blocker 1: Tool Output recording tests.

Verifies that the generic executor captures tool output via set_output(),
producing payload.output, tool.output.type, tool.output.size_bytes, and
tool.output.truncated attributes.
"""
import sys, os, asyncio
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
    Observability.init(app_name="tool-output-test", endpoint="http://localhost:99999")
    ctx = SpanContext(
        trace_id="0" * 32, span_id="a" * 16,
        parent_span_id=None, span_kind="AGENT", sampled=True,
    )
    token = set_context(ctx)
    yield Observability._tracer
    reset_context(token)
    Observability.shutdown()


def _make_tool_request(name="search", args=None, call_id="tc_001"):
    request = MagicMock()
    request.tool_call = {"name": name, "args": args or {"query": "test"}, "id": call_id}
    request.tool = MagicMock()
    request.tool.name = name
    return request


def test_langchain_tool_result_records_output_payload(init_sdk):
    """Tool handler returns a dict result → payload.output + tool.output.* are set."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = _make_tool_request()
    expected_output = {"result": "found 3 items", "items": ["a", "b", "c"]}
    handler = MagicMock(return_value=expected_output)

    result = mw.wrap_tool_call(request, handler)

    tracer.reporter.report = orig
    assert result is expected_output

    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    assert len(tool_spans) == 1, f"Expected 1 TOOL span, got {len(tool_spans)}"
    span = tool_spans[0]

    # payload.output must exist
    payload = span.get("payload") or {}
    assert "output" in payload, f"payload.output missing — payload keys: {list(payload.keys())}"
    assert payload["output"]["result"] == "found 3 items"

    # tool.output.* attributes
    attrs = span.get("attributes", {})
    assert "tool.output.type" in attrs, "tool.output.type missing"
    assert attrs["tool.output.type"] == "dict"
    assert "tool.output.size_bytes" in attrs, "tool.output.size_bytes missing"
    assert attrs["tool.output.size_bytes"] > 0
    assert "tool.output.truncated" in attrs, "tool.output.truncated missing"
    assert attrs["tool.output.truncated"] is False


def test_langchain_tool_none_result_records_null(init_sdk):
    """Tool handler returns None → output is recorded as None type."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = _make_tool_request()
    handler = MagicMock(return_value=None)

    result = mw.wrap_tool_call(request, handler)

    tracer.reporter.report = orig
    assert result is None

    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    assert len(tool_spans) == 1
    span = tool_spans[0]

    payload = span.get("payload") or {}
    assert "output" in payload, "payload.output missing for None result"
    assert payload["output"] is None

    attrs = span.get("attributes", {})
    assert attrs.get("tool.output.type") == "NoneType"


def test_langchain_async_tool_result_records_output(init_sdk):
    """Async tool handler returns a result → output is captured via set_output."""
    tracer = init_sdk
    captured = []
    orig = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    mw = LangChainObservabilityMiddleware()
    request = _make_tool_request()

    async def async_handler(req):
        return {"status": "done", "count": 42}

    result = asyncio.run(mw.awrap_tool_call(request, async_handler))

    tracer.reporter.report = orig
    assert result == {"status": "done", "count": 42}

    tool_spans = [r for r in captured if r["span_kind"] == "TOOL"]
    assert len(tool_spans) == 1
    span = tool_spans[0]

    payload = span.get("payload") or {}
    assert "output" in payload, "payload.output missing in async tool"
    assert payload["output"]["status"] == "done"
    assert payload["output"]["count"] == 42

    attrs = span.get("attributes", {})
    assert attrs.get("tool.output.type") == "dict"
