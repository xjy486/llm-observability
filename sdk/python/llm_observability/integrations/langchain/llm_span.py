"""Logical LLM Span for LangChain Middleware.

Creates an LLM span child of the current context, with
logical_llm_span_active=True for OpenAI Instrumentor dedup.

Provider-neutral: works with any BaseChatModel.
"""
import logging
from typing import Any, Optional

from ...context import SpanContext, get_current_context, set_context, reset_context
from ...spans import Span, SpanKind
from ...utils.ids import generate_span_id
from ...tool import safe_serialize, apply_size_guard
from ...utils.masking import mask_payload
from .metadata import extract_model_attributes, normalize_messages, extract_token_usage

logger = logging.getLogger("llm_obs.integrations.langchain.llm_span")


class LLMSpanHandle:
    """Handle for setting LLM span response data."""

    def __init__(self, span: Optional[Span], sampled: bool, tracer=None):
        self._span = span
        self._sampled = sampled
        self._tracer = tracer

    def set_response(self, response: Any):
        """Set the model response for token usage and output payload extraction."""
        if self._span is None or not self._sampled:
            return
        try:
            # Extract token usage
            usage = extract_token_usage(response)
            for k, v in usage.items():
                self._span.set_attribute(k, v)

            # Rework P0-7: the LLM span keeps its LOGICAL response usage; the
            # Router aggregate is never written back via ContextVar (SDK and
            # gateway commonly run in separate processes). Retry waste is
            # derived by Core/UI from the trace tree.

            # Store response payload (masked)
            strategy = self._tracer.config.payload_strategy if self._tracer else "off"
            if strategy != "off":
                try:
                    messages = getattr(response, "result", None)
                    if messages:
                        normalized = normalize_messages(messages)
                        masked = mask_payload(normalized, strategy)
                        guarded, truncated, orig_size = apply_size_guard(masked)
                        if self._span.payload is None:
                            self._span.payload = {}
                        self._span.payload["output"] = guarded
                except Exception as e:
                    logger.debug("LLM output payload failed: %s", e)
        except Exception as e:
            logger.debug("set_response failed: %s", e)


class LogicalLLMSpan:
    """Context manager for a logical LLM span.

    When no active context exists, this is a noop (spec §15).
    When active, creates an LLM child span with logical_llm_span_active=True.
    """

    def __init__(self, request: Any):
        self._request = request
        self._span: Optional[Span] = None
        self._token = None
        self._sampled = True
        self._handle: Optional[LLMSpanHandle] = None
        self._tracer = None

    def _get_tracer(self):
        from llm_observability import Observability
        if Observability._tracer is not None:
            return Observability._tracer
        return None

    def __enter__(self) -> LLMSpanHandle:
        current = get_current_context()
        if current is None:
            # No active context — noop (spec §15)
            logger.debug("No active context — LLM span is noop")
            self._handle = LLMSpanHandle(None, False)
            return self._handle

        self._tracer = self._get_tracer()
        self._sampled = current.sampled

        span_id = generate_span_id()
        ctx = SpanContext(
            trace_id=current.trace_id,
            span_id=span_id,
            parent_span_id=current.span_id,
            span_kind=SpanKind.LLM,
            sampled=current.sampled,
            logical_llm_span_active=True,
        )

        span = Span(
            trace_id=current.trace_id,
            span_id=span_id,
            parent_span_id=current.span_id,
            span_name="llm.completion",
            span_kind=SpanKind.LLM,
        )

        # Extract attributes
        try:
            attrs = extract_model_attributes(self._request)
            for k, v in attrs.items():
                span.set_attribute(k, v)
        except Exception as e:
            logger.debug("Model attributes extraction failed: %s", e)

        # Input payload (masked)
        if self._tracer and self._sampled:
            strategy = self._tracer.config.payload_strategy
            if strategy != "off":
                try:
                    messages = getattr(self._request, "messages", None)
                    if messages:
                        normalized = normalize_messages(messages)
                        masked = mask_payload(normalized, strategy)
                        guarded, truncated, orig_size = apply_size_guard(masked)
                        if span.payload is None:
                            span.payload = {}
                        span.payload["input"] = guarded
                except Exception as e:
                    logger.debug("LLM input payload failed: %s", e)

        span.start()

        try:
            self._token = set_context(ctx)
        except Exception:
            try:
                span.end()
            except Exception:
                pass
            raise

        self._span = span
        self._handle = LLMSpanHandle(span, self._sampled, self._tracer)
        return self._handle

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._span is None:
            return False

        try:
            if exc_type is not None:
                # P1-2: GraphInterrupt is human-in-the-loop control flow, not an error
                from .compat import is_langgraph_interrupt
                if is_langgraph_interrupt(exc_val):
                    self._span.set_attribute("langchain.interrupted", True)
                    self._span.set_attribute("langchain.interrupt.type", type(exc_val).__name__)
                    # Do NOT set ERROR — interrupt is not a failure
                else:
                    self._span.set_error(
                        error_type=exc_type.__name__,
                        error_message=str(exc_val),
                    )
            else:
                if self._span.status != "ERROR":
                    self._span.set_status("OK")

            self._span.end()

            if self._sampled and self._tracer:
                try:
                    self._tracer.reporter.report(self._span.to_record())
                except Exception as e:
                    logger.error("Failed to report LLM span: %s", e)
        finally:
            # P0-2: Context MUST be restored even if span/reporter fails
            if self._token is not None:
                reset_context(self._token)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__exit__(exc_type, exc_val, exc_tb)
