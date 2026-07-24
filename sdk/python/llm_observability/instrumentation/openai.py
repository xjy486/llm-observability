"""OpenAI SDK instrumentation.

Patches openai.resources.chat.completions.Completions.create to:
1. Create a logical LLM span as child of the current context
2. Inject traceparent + ownership marker headers
3. Record response metadata (model, tokens)
4. Handle dedup via logical_llm_span_active flag (spec §19)
"""
import logging
from typing import Optional
from functools import wraps

from .base import BaseInstrumentor
from ..context import SpanContext, get_current_context, set_context, reset_context
from ..spans import Span, SpanKind
from ..propagation import inject_headers
from ..utils.ids import generate_span_id

logger = logging.getLogger("llm_obs.instrumentation.openai")

# Module-level reference to the tracer, set during instrument()
_tracer = None
_original_create = None


class OpenAIInstrumentor(BaseInstrumentor):
    """Instruments OpenAI SDK's chat.completions.create."""

    def instrument(self, tracer=None, **kwargs):
        """Patch OpenAI chat.completions.create.

        Args:
            tracer: The Tracer instance to use for span creation.
        """
        global _tracer, _original_create
        if self._patched:
            return

        try:
            import openai
        except ImportError:
            logger.warning("openai package not installed — cannot instrument")
            return

        _tracer = tracer
        target = openai.resources.chat.completions.Completions
        _original_create = target.create
        target.create = _patched_create
        self._patched = True
        logger.info("OpenAI instrumentation installed")

    def uninstrument(self):
        """Restore original OpenAI create."""
        global _tracer, _original_create
        if not self._patched:
            return

        try:
            import openai
            target = openai.resources.chat.completions.Completions
            if _original_create is not None:
                target.create = _original_create
        except ImportError:
            pass

        _tracer = None
        _original_create = None
        self._patched = False
        logger.info("OpenAI instrumentation removed")


def _patched_create(self, *args, **kwargs):
    """Patched version of chat.completions.create.

    Creates an LLM span, injects headers, calls original, records result.
    Dedup: if logical_llm_span_active is True, skips span creation.
    """
    if _tracer is None or _original_create is None:
        return _original_create(self, *args, **kwargs)

    current_ctx = get_current_context()

    # Dedup: skip if a logical LLM span is already active (spec §19)
    if current_ctx and current_ctx.logical_llm_span_active:
        return _original_create(self, *args, **kwargs)

    # If no active context at all, still call original (no trace)
    if current_ctx is None:
        return _original_create(self, *args, **kwargs)

    # Create LLM span
    span_id = generate_span_id()
    llm_ctx = SpanContext(
        trace_id=current_ctx.trace_id,
        span_id=span_id,
        parent_span_id=current_ctx.span_id,
        span_kind=SpanKind.LLM,
        sampled=current_ctx.sampled,
        logical_llm_span_active=True,
    )
    token = set_context(llm_ctx)

    span = Span(
        trace_id=current_ctx.trace_id,
        span_id=span_id,
        parent_span_id=current_ctx.span_id,
        span_name="llm.completion",
        span_kind=SpanKind.LLM,
    )

    # Extract request info for attributes
    model = kwargs.get("model", "unknown")
    messages = kwargs.get("messages", [])
    stream = kwargs.get("stream", False)

    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("llm.stream", stream)

    # Payload (masked per config strategy)
    if _tracer.config.payload_strategy != "off":
        from ..utils.masking import mask_payload
        span.set_attribute("llm.input.messages", mask_payload(messages, _tracer.config.payload_strategy))

    span.start()

    # Inject traceparent + ownership marker into headers
    headers = kwargs.pop("extra_headers", None) or {}
    inject = inject_headers(
        llm_ctx,
        is_logical_llm=True,
    )
    headers.update(inject)
    kwargs["extra_headers"] = headers

    try:
        response = _original_create(self, *args, **kwargs)
        span.set_status("OK")

        # Extract response metadata
        if hasattr(response, "model") and response.model:
            span.set_attribute("gen_ai.response.model", response.model)
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            if hasattr(usage, "prompt_tokens") and usage.prompt_tokens is not None:
                span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
            if hasattr(usage, "completion_tokens") and usage.completion_tokens is not None:
                span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
            if hasattr(usage, "total_tokens") and usage.total_tokens is not None:
                span.set_attribute("gen_ai.usage.total_tokens", usage.total_tokens)

        # Store response payload (masked)
        if _tracer.config.payload_strategy != "off" and response:
            from ..utils.masking import mask_payload
            try:
                resp_dict = response.model_dump() if hasattr(response, "model_dump") else None
                if resp_dict:
                    span.set_attribute("llm.output", mask_payload(resp_dict, _tracer.config.payload_strategy))
            except Exception:
                pass

        return response

    except Exception as e:
        span.set_error(error_type=type(e).__name__, error_message=str(e))
        raise

    finally:
        span.end()
        try:
            _tracer.reporter.report(span.to_record())
        except Exception as e:
            logger.error("Failed to report LLM span: %s", e)
        reset_context(token)
