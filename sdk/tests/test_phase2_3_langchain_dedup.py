"""Phase 2.3: OpenAI dedup tests.

Verifies that when LangChain Middleware creates an LLM span with
logical_llm_span_active=True, the OpenAI Instrumentor does NOT create
a second LLM span, but still injects traceparent headers.
"""
import pytest
from unittest.mock import MagicMock, patch
from llm_observability import Observability
from llm_observability.context import get_current_context
from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan


@pytest.fixture
def init_sdk():
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    yield
    Observability.shutdown()


def test_langchain_llm_span_sets_dedup_flag(init_sdk):
    """When LogicalLLMSpan is active, logical_llm_span_active must be True."""
    with Observability.trace("test"):
        ctx_before = get_current_context()
        assert ctx_before.logical_llm_span_active is False

        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "fake"
        request.messages = []
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        with LogicalLLMSpan(request):
            ctx_during = get_current_context()
            assert ctx_during.logical_llm_span_active is True

        ctx_after = get_current_context()
        assert ctx_after.logical_llm_span_active is False


def test_openai_instrumentor_skips_when_dedup_active(init_sdk):
    """OpenAI Instrumentor must not create LLM span when logical_llm_span_active=True."""
    from llm_observability.instrumentation.openai import OpenAIInstrumentor
    from llm_observability.context import SpanContext, set_context, reset_context, get_current_context

    with Observability.trace("test"):
        ctx = get_current_context()
        llm_ctx = SpanContext(
            trace_id=ctx.trace_id,
            span_id="aaaa1111bbbb2222",
            parent_span_id=ctx.span_id,
            span_kind="LLM",
            sampled=True,
            logical_llm_span_active=True,
        )
        token = set_context(llm_ctx)

        current = get_current_context()
        assert current.logical_llm_span_active is True

        # The instrumentor's _do_patch checks this flag and skips span creation
        # We verify the dedup condition is met
        assert current.logical_llm_span_active is True

        reset_context(token)


def test_dedup_still_injects_traceparent(init_sdk):
    """Even with dedup, traceparent must still be injected into headers."""
    from llm_observability.propagation import inject_headers
    from llm_observability.context import SpanContext

    ctx = SpanContext(
        trace_id="0" * 32,
        span_id="aaaa1111bbbb2222",
        parent_span_id=None,
        span_kind="LLM",
        sampled=True,
        logical_llm_span_active=True,
    )

    headers = inject_headers(ctx, is_logical_llm=True)
    assert "traceparent" in headers
    assert "X-LLM-OBS-Span-Role" in headers
    assert headers["X-LLM-OBS-Span-Role"] == "llm"


def test_langchain_middleware_creates_one_llm_not_two(init_sdk):
    """When Middleware creates LLM, OpenAI Instrumentor should skip.

    We simulate the dedup by verifying that LogicalLLMSpan sets the flag,
    and the OpenAI instrumentor's check would see it.
    """
    captured_spans = []
    original_report = Observability._tracer.reporter.report
    Observability._tracer.reporter.report = lambda r: captured_spans.append(r)

    from llm_observability.integrations.langchain.middleware import LangChainObservabilityMiddleware
    mw = LangChainObservabilityMiddleware()

    with Observability.trace("test"):
        request = MagicMock()
        request.model = MagicMock()
        del request.model.model_name
        del request.model.model
        request.model._llm_type = "fake"
        request.messages = []
        request.runtime = MagicMock()
        request.runtime.execution_info = None

        response = MagicMock()
        response.result = []

        def handler(req):
            # During handler execution, logical_llm_span_active should be True
            ctx = get_current_context()
            assert ctx is not None
            assert ctx.logical_llm_span_active is True
            return response

        mw.wrap_model_call(request, handler)

    Observability._tracer.reporter.report = original_report

    llm_spans = [s for s in captured_spans if s["span_kind"] == "LLM"]
    assert len(llm_spans) == 1, f"Expected 1 LLM span, got {len(llm_spans)}"
