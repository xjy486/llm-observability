"""OpenAI SDK instrumentation.

Patches openai.resources.chat.completions.Completions.create to:
1. Create a logical LLM span as child of the current context
2. Inject traceparent + ownership marker headers
3. Record response metadata (model, tokens)
4. Handle dedup via logical_llm_span_active flag (spec §19)

P0-1 (final): ContextVar lifetime is decoupled from Span lifetime.
              Context is restored immediately after original_create() returns.
              ObservedStream only manages Span lifecycle, not context.
P0-2 (final): Sampling decision inherited from parent context; LLM span
              finalize gates reporter.report on sampled flag.
P0-3: Streaming responses are wrapped in ObservedStream to defer span
      finalization until the stream is fully consumed.
P1-4: Dedup still injects traceparent + ownership marker headers.
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


class OpenAIInstrumentor(BaseInstrumentor):
    """Instruments OpenAI SDK's chat.completions.create.

    P0-2: All state is instance-level to support correct init/shutdown/re-init.
    """

    def __init__(self):
        super().__init__()
        self._tracer = None
        self._original_create = None

    def instrument(self, tracer=None, **kwargs):
        """Patch OpenAI chat.completions.create.

        Args:
            tracer: The Tracer instance to use for span creation.
        """
        if self._patched:
            return

        try:
            import openai
        except ImportError:
            logger.warning("openai package not installed — cannot instrument")
            return

        self._tracer = tracer
        target = openai.resources.chat.completions.Completions
        self._original_create = target.create

        # Use a closure that captures self for instance-level state access
        instrumentor = self

        def _patched(self_inner, *args, **kwargs):
            return instrumentor._do_patch(self_inner, *args, **kwargs)

        target.create = _patched
        self._patched = True
        logger.info("OpenAI instrumentation installed")

    def uninstrument(self):
        """Restore original OpenAI create."""
        if not self._patched:
            return

        try:
            import openai
            target = openai.resources.chat.completions.Completions
            if self._original_create is not None:
                target.create = self._original_create
        except ImportError:
            pass

        self._tracer = None
        self._original_create = None
        self._patched = False
        logger.info("OpenAI instrumentation removed")

    def _do_patch(self, self_inner, *args, **kwargs):
        """Patched version of chat.completions.create.

        Creates an LLM span, injects headers, calls original, records result.
        P1-4: Dedup — if logical_llm_span_active is True, skips span creation
              but STILL injects traceparent + ownership marker.
        P0-1: Context is restored immediately after create() returns.
        P0-2: Sampling inherited from parent; report gated on sampled.
        """
        if self._tracer is None or self._original_create is None:
            return self._original_create(self_inner, *args, **kwargs)

        current_ctx = get_current_context()

        # If no active context at all, call original (no trace)
        if current_ctx is None:
            return self._original_create(self_inner, *args, **kwargs)

        stream = kwargs.get("stream", False)

        # P1-4: Dedup — skip span creation but still propagate context
        if current_ctx.logical_llm_span_active:
            # Still inject traceparent + ownership marker
            headers = kwargs.pop("extra_headers", None) or {}
            inject = inject_headers(
                current_ctx,
                is_logical_llm=True,
            )
            headers.update(inject)
            kwargs["extra_headers"] = headers
            return self._original_create(self_inner, *args, **kwargs)

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

        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("llm.stream", stream)

        # Payload (masked per config strategy)
        if self._tracer.config.payload_strategy != "off":
            from ..utils.masking import mask_payload
            span.set_attribute(
                "llm.input.messages",
                mask_payload(messages, self._tracer.config.payload_strategy),
            )

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
            response = self._original_create(self_inner, *args, **kwargs)
        except Exception as e:
            # Error path: finalize span and reset context (context not yet restored)
            span.set_error(error_type=type(e).__name__, error_message=str(e))
            self._finalize_span(span, token, sampled=llm_ctx.sampled)
            raise

        # P0-1: Restore parent context IMMEDIATELY after create() returns.
        # The ContextVar activation was only needed for header injection.
        # Span lifetime continues independently (streaming) or ends now (non-streaming).
        reset_context(token)

        # P0-3: For streaming, wrap in ObservedStream (no token needed)
        if stream:
            return ObservedStream(
                response,
                span,
                self._tracer,
                sampled=llm_ctx.sampled,
            )

        # Non-streaming: process and finalize immediately
        span.set_status("OK")
        self._extract_response_metadata(span, response)

        # Store response payload (masked)
        if self._tracer.config.payload_strategy != "off" and response:
            from ..utils.masking import mask_payload
            try:
                resp_dict = response.model_dump() if hasattr(response, "model_dump") else None
                if resp_dict:
                    span.set_attribute(
                        "llm.output",
                        mask_payload(resp_dict, self._tracer.config.payload_strategy),
                    )
            except Exception:
                pass

        self._finalize_span_no_reset(span, sampled=llm_ctx.sampled)
        return response

    def _extract_response_metadata(self, span: Span, response):
        """Extract model and usage from response."""
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

    def _finalize_span(self, span: Span, token, sampled: bool = True):
        """End and report a span, then reset context. Used for error path only.

        P0-2: Gates reporter.report on sampled flag.
        """
        span.end()
        if sampled:
            try:
                self._tracer.reporter.report(span.to_record())
            except Exception as e:
                logger.error("Failed to report LLM span: %s", e)
        reset_context(token)

    def _finalize_span_no_reset(self, span: Span, sampled: bool = True):
        """End and report a span WITHOUT resetting context (P0-1: context already restored).

        P0-2: Gates reporter.report on sampled flag.
        """
        span.end()
        if sampled:
            try:
                self._tracer.reporter.report(span.to_record())
            except Exception as e:
                logger.error("Failed to report LLM span: %s", e)


class ObservedStream:
    """Wrapper for OpenAI streaming responses.

    P0-1 (final): ContextVar lifetime is decoupled from Span lifetime.
    The ContextVar is restored by the caller (_do_patch) immediately after
    original_create() returns. ObservedStream only manages the Span lifecycle
    (start/end/report), NOT the context.

    P0-2: Reporting is gated on the sampled flag.

    The span is finalized when:
    - The iterator is exhausted (normal completion)
    - close() is called
    - An exception occurs during iteration
    - __del__ best-effort (for early break without close)

    Maintains original OpenAI Stream behavior — all attributes and methods
    are delegated to the wrapped stream.
    """

    def __init__(self, stream, span: Span, tracer, sampled: bool = True):
        self._stream = stream
        self._span = span
        self._tracer = tracer
        self._sampled = sampled
        self._finalized = False
        self._collected_content = []

    def _finalize(self, error: Optional[Exception] = None):
        """Finalize the span — end, report. Does NOT reset context (P0-1)."""
        if self._finalized:
            return
        self._finalized = True

        if error is not None:
            self._span.set_error(
                error_type=type(error).__name__,
                error_message=str(error),
            )
        else:
            self._span.set_status("OK")

        # Try to extract usage from the stream's last chunk
        self._try_extract_stream_usage()

        # Store aggregated content if available
        if self._tracer.config.payload_strategy != "off" and self._collected_content:
            from ..utils.masking import mask_payload
            try:
                content = "".join(self._collected_content)
                self._span.set_attribute(
                    "llm.output",
                    mask_payload({"content": content}, self._tracer.config.payload_strategy),
                )
            except Exception:
                pass

        self._span.end()

        # P0-2: Only report if sampled
        if self._sampled:
            try:
                self._tracer.reporter.report(self._span.to_record())
            except Exception as e:
                logger.error("Failed to report LLM span: %s", e)

    def _try_extract_stream_usage(self):
        """Try to extract usage info from stream attributes."""
        try:
            if hasattr(self, "_last_usage") and self._last_usage:
                usage = self._last_usage
                if hasattr(usage, "prompt_tokens") and usage.prompt_tokens is not None:
                    self._span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
                if hasattr(usage, "completion_tokens") and usage.completion_tokens is not None:
                    self._span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
                if hasattr(usage, "total_tokens") and usage.total_tokens is not None:
                    self._span.set_attribute("gen_ai.usage.total_tokens", usage.total_tokens)
        except Exception:
            pass

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._stream)
            # Collect content deltas for potential usage
            try:
                if hasattr(chunk, "choices") and chunk.choices:
                    delta = chunk.choices[0].delta if hasattr(chunk.choices[0], "delta") else None
                    if delta and hasattr(delta, "content") and delta.content:
                        self._collected_content.append(delta.content)
                # Check for usage in this chunk
                if hasattr(chunk, "usage") and chunk.usage:
                    self._last_usage = chunk.usage
            except Exception:
                pass
            return chunk
        except StopIteration:
            self._finalize()
            raise
        except Exception as e:
            self._finalize(error=e)
            raise

    def close(self):
        """Close the stream and finalize the span."""
        try:
            if hasattr(self._stream, "close"):
                self._stream.close()
        except Exception:
            pass
        self._finalize()

    def __getattr__(self, name):
        """Delegate attribute access to the wrapped stream."""
        return getattr(self._stream, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._finalize(error=exc_val)
        else:
            self._finalize()
        return False

    def __del__(self):
        """Best-effort finalize for early-break scenarios. Do NOT rely on this."""
        if not self._finalized:
            try:
                self._finalize()
            except Exception:
                pass