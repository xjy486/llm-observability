"""Phase 2.3: Logical LLM Span tests."""
import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import get_current_context
from langchain_core.messages import HumanMessage, AIMessage


@pytest.fixture
def init_sdk():
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    yield
    Observability.shutdown()


def test_llm_span_creates_llm_child(init_sdk):
    from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan
    from llm_observability import Observability

    with Observability.trace("test-trace"):
        ctx_before = get_current_context()
        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "gpt-4o"
        request.messages = [HumanMessage(content="hello")]
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        with LogicalLLMSpan(request) as span:
            ctx_during = get_current_context()
            assert ctx_during is not None
            assert ctx_during.span_kind == "LLM"
            assert ctx_during.logical_llm_span_active is True
            assert ctx_during.parent_span_id == ctx_before.span_id
            span.set_response(MagicMock())


def test_llm_span_context_restored(init_sdk):
    from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan
    from llm_observability import Observability

    with Observability.trace("test-trace"):
        ctx_before = get_current_context()
        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "fake"
        request.messages = []
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        with LogicalLLMSpan(request):
            pass  # no set_response

        ctx_after = get_current_context()
        assert ctx_after.span_id == ctx_before.span_id
        assert ctx_after.logical_llm_span_active is False


def test_llm_span_error_marks_error(init_sdk):
    from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan
    from llm_observability import Observability

    with Observability.trace("test-trace"):
        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "fake"
        request.messages = []
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        with pytest.raises(ValueError):
            with LogicalLLMSpan(request):
                raise ValueError("test error")


def test_llm_span_no_active_context_is_noop():
    from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan
    request = MagicMock()
    request.model = MagicMock()
    request.messages = []
    request.runtime = MagicMock()

    # No active trace — should not raise, should be noop
    with LogicalLLMSpan(request) as span:
        pass  # should work without error


def test_llm_span_token_usage_recorded(init_sdk):
    from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan
    from llm_observability import Observability

    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    with Observability.trace("test-trace"):
        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "fake"
        request.messages = []
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        response = MagicMock()
        msg = MagicMock()
        msg.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        response.result = [msg]

        with LogicalLLMSpan(request) as span:
            span.set_response(response)

    Observability._tracer.reporter.report = original_report

    llm_spans = [s for s in captured_spans if s["span_kind"] == "LLM"]
    assert len(llm_spans) == 1
    assert llm_spans[0]["attributes"].get("gen_ai.usage.input_tokens") == 10
    assert llm_spans[0]["attributes"].get("gen_ai.usage.output_tokens") == 5
    assert llm_spans[0]["attributes"].get("gen_ai.usage.total_tokens") == 15
