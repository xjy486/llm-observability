"""Phase 2.3: LangChain Middleware tests (tool + model)."""
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from llm_observability import Observability
from llm_observability.context import get_current_context
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage


@pytest.fixture
def init_sdk():
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    yield
    Observability.shutdown()


def test_tool_call_creates_tool_child(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    from llm_observability import Observability

    mw = LangChainObservabilityMiddleware()

    with Observability.trace("test-trace"):
        ctx_before = get_current_context()

        request = MagicMock()
        request.tool_call = {"name": "search", "args": {"q": "test"}, "id": "c1", "type": "tool_call"}
        request.tool = MagicMock()
        request.tool.name = "search"

        def handler(req):
            return ToolMessage(content="result", tool_call_id="c1")

        result = mw.wrap_tool_call(request, handler)
        assert result.content == "result"

        ctx_after = get_current_context()
        assert ctx_after.span_id == ctx_before.span_id


def test_tool_call_error_reraised(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    from llm_observability import Observability

    mw = LangChainObservabilityMiddleware()

    with Observability.trace("test-trace"):
        request = MagicMock()
        request.tool_call = {"name": "search", "args": {}, "id": "c1", "type": "tool_call"}
        request.tool = None

        def handler(req):
            raise ValueError("tool error")

        with pytest.raises(ValueError):
            mw.wrap_tool_call(request, handler)


def test_tool_call_id_recorded(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    from llm_observability import Observability

    mw = LangChainObservabilityMiddleware()

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    def mock_report(record):
        captured_spans.append(record)

    Observability._tracer.reporter.report = mock_report
    try:
        with Observability.trace("test-trace"):
            request = MagicMock()
            request.tool_call = {"name": "search", "args": {"q": "x"}, "id": "call_abc", "type": "tool_call"}
            request.tool = None

            def handler(req):
                return ToolMessage(content="ok", tool_call_id="call_abc")

            mw.wrap_tool_call(request, handler)
    finally:
        Observability._tracer.reporter.report = original_report

    tool_spans = [s for s in captured_spans if s["span_kind"] == "TOOL"]
    assert len(tool_spans) == 1
    assert tool_spans[0]["attributes"].get("tool.call_id") == "call_abc"


def test_no_active_context_tool_hook_is_noop(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware

    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    request.tool_call = {"name": "search", "args": {}, "id": "c1", "type": "tool_call"}
    request.tool = None

    def handler(req):
        return ToolMessage(content="ok", tool_call_id="c1")

    # No active trace — should not raise, should pass through
    result = mw.wrap_tool_call(request, handler)
    assert result.content == "ok"


def test_model_call_creates_llm_child(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    from llm_observability import Observability

    mw = LangChainObservabilityMiddleware()

    with Observability.trace("test-trace"):
        ctx_before = get_current_context()

        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "fake"
        request.messages = [HumanMessage(content="hi")]
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        response = MagicMock()
        response.result = [AIMessage(content="hello")]

        def handler(req):
            return response

        result = mw.wrap_model_call(request, handler)
        assert result is response

        ctx_after = get_current_context()
        assert ctx_after.span_id == ctx_before.span_id
        assert ctx_after.logical_llm_span_active is False


def test_model_error_marks_llm_error(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    from llm_observability import Observability

    mw = LangChainObservabilityMiddleware()

    with Observability.trace("test-trace"):
        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "fake"
        request.messages = []
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        def handler(req):
            raise RuntimeError("model error")

        with pytest.raises(RuntimeError):
            mw.wrap_model_call(request, handler)


def test_no_active_context_model_hook_is_noop(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware

    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    request.model = MagicMock()
    del request.model.model_name
    del request.model.model
    request.model._llm_type = "fake"
    request.messages = []
    request.runtime = MagicMock()
    request.runtime.execution_info = None

    response = MagicMock()
    response.result = [AIMessage(content="hi")]

    def handler(req):
        return response

    # No active trace
    result = mw.wrap_model_call(request, handler)
    assert result is response


def test_async_tool_call_creates_tool_child(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    from llm_observability import Observability

    mw = LangChainObservabilityMiddleware()

    async def run_test():
        with Observability.trace("test-trace"):
            ctx_before = get_current_context()

            request = MagicMock()
            request.tool_call = {"name": "search", "args": {}, "id": "c1", "type": "tool_call"}
            request.tool = None

            async def handler(req):
                return ToolMessage(content="ok", tool_call_id="c1")

            result = await mw.awrap_tool_call(request, handler)
            assert result.content == "ok"

            ctx_after = get_current_context()
            assert ctx_after.span_id == ctx_before.span_id

    asyncio.run(run_test())


def test_async_model_call_creates_llm_child(init_sdk):
    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    from llm_observability import Observability

    mw = LangChainObservabilityMiddleware()

    async def run_test():
        with Observability.trace("test-trace"):
            request = MagicMock()
            request.model = MagicMock()
            del request.model.model_name
            del request.model.model
            request.model._llm_type = "fake"
            request.messages = [HumanMessage(content="hi")]
            request.runtime = MagicMock()
            request.runtime.execution_info = None

            response = MagicMock()
            response.result = [AIMessage(content="hello")]

            async def handler(req):
                return response

            result = await mw.awrap_model_call(request, handler)
            assert result is response

    asyncio.run(run_test())
