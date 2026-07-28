"""P0-2: Fail-open and context safety tests."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock, patch
from llm_observability import Observability
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context
from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="failopen-test", endpoint="http://localhost:99999")
    ctx = SpanContext(trace_id="0"*32, span_id="a"*16, parent_span_id=None, span_kind="AGENT", sampled=True)
    token = set_context(ctx)
    yield Observability._tracer
    reset_context(token)
    Observability.shutdown()


def test_model_instrumentation_enter_failure_still_calls_handler(init_sdk):
    """If LogicalLLMSpan.__enter__ fails, handler still executes."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    handler_called = [0]
    expected_result = MagicMock()

    def handler(req):
        handler_called[0] += 1
        return expected_result

    with patch.object(LogicalLLMSpan, "__enter__", side_effect=RuntimeError("init boom")):
        result = mw.wrap_model_call(request, handler)

    assert handler_called[0] == 1
    assert result is expected_result


def test_model_instrumentation_exit_failure_preserves_result(init_sdk):
    """If __exit__ fails, business return value is preserved."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    expected_result = MagicMock()
    handler = MagicMock(return_value=expected_result)

    with patch.object(LogicalLLMSpan, "__exit__", side_effect=RuntimeError("exit boom")):
        result = mw.wrap_model_call(request, handler)

    assert result is expected_result


def test_model_instrumentation_exit_failure_preserves_business_exception(init_sdk):
    """If __exit__ fails, business exception is still raised."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    business_error = ValueError("business fail")
    handler = MagicMock(side_effect=business_error)

    with patch.object(LogicalLLMSpan, "__exit__", side_effect=RuntimeError("exit boom")):
        with pytest.raises(ValueError, match="business fail"):
            mw.wrap_model_call(request, handler)


def test_model_context_restored_when_error_stringification_fails(init_sdk):
    """Context is restored even if str(exc_val) raises."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()

    class BadException(Exception):
        def __str__(self):
            raise RuntimeError("str failed")

    handler = MagicMock(side_effect=BadException())

    parent_ctx = get_current_context()
    with pytest.raises(BadException):
        mw.wrap_model_call(request, handler)

    # Context must be restored to parent
    assert get_current_context() is parent_ctx


def test_tool_instrumentation_enter_failure_still_calls_handler(init_sdk):
    """If tool instrumentation init fails, handler still executes."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    handler_called = [0]
    expected_result = MagicMock()

    def handler(req):
        handler_called[0] += 1
        return expected_result

    with patch("llm_observability.integrations.langchain.middleware.extract_tool_name", side_effect=RuntimeError("init boom")):
        result = mw.wrap_tool_call(request, handler)

    assert handler_called[0] == 1
    assert result is expected_result


def test_tool_instrumentation_exit_failure_preserves_result(init_sdk):
    """If tool __exit__ fails, business result is preserved."""
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    expected_result = MagicMock()
    handler = MagicMock(return_value=expected_result)

    with patch("llm_observability.tool.ToolContextManager.__exit__", side_effect=RuntimeError("exit boom")):
        result = mw.wrap_tool_call(request, handler)

    assert result is expected_result


def test_tool_context_restored_on_instrumentation_failure(init_sdk):
    """Context restored even if reporter fails during tool exit."""
    tracer = init_sdk
    mw = LangChainObservabilityMiddleware()
    request = MagicMock()
    handler = MagicMock(return_value="ok")
    parent_ctx = get_current_context()

    with patch.object(tracer.reporter, "report", side_effect=RuntimeError("reporter dead")):
        result = mw.wrap_tool_call(request, handler)

    assert result == "ok"
    assert get_current_context() is parent_ctx
