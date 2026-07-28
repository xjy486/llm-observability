"""Callback-driven LLM and Tool spans for Phase 2.4.

CallbackLLMSpan creates an LLM span from LangChain callback parameters
(serialized dict, invocation_params) — distinct from Phase 2.3's
LogicalLLMSpan which uses ModelRequest.

Sets logical_llm_span_active=True for OpenAI Instrumentor dedup.
"""
import logging
import time
from typing import Any, Optional

from ...context import SpanContext, set_context, reset_context
from ...spans import Span, SpanKind
from ...utils.ids import generate_span_id
from ...tool import safe_serialize, apply_size_guard
from ...utils.masking import mask_payload

logger = logging.getLogger("llm_obs.integrations.langchain.callback_spans")


def _extract_model_info(serialized: dict, invocation_params: dict) -> dict:
    """Extract model name and provider from callback serialized/invocation params."""
    info = {}
    try:
        model = invocation_params.get("model") if invocation_params else None
        if not model and serialized:
            ids = serialized.get("id", [])
            if ids:
                model = ids[-1]
        if model:
            info["gen_ai.request.model"] = str(model)

        name = serialized.get("name", "") if serialized else ""
        name_lower = name.lower() if name else ""
        if "openai" in name_lower:
            info["gen_ai.provider.name"] = "openai"
        elif "anthropic" in name_lower:
            info["gen_ai.provider.name"] = "anthropic"
        elif "google" in name_lower or "gemini" in name_lower:
            info["gen_ai.provider.name"] = "google"

        info["gen_ai.operation.name"] = "chat"
    except Exception as e:
        logger.debug("Model info extraction failed: %s", e)
    return info


def _extract_token_usage_from_response(response: Any) -> dict:
    """Extract token usage from LLM response (LLMResult or AIMessage)."""
    usage = {}
    try:
        # LLMResult.llm_output
        llm_output = getattr(response, "llm_output", None)
        if llm_output and isinstance(llm_output, dict):
            tu = llm_output.get("token_usage") or llm_output.get("usage")
            if tu:
                for src, dst in [
                    ("prompt_tokens", "gen_ai.usage.input_tokens"),
                    ("completion_tokens", "gen_ai.usage.output_tokens"),
                    ("total_tokens", "gen_ai.usage.total_tokens"),
                ]:
                    if src in tu:
                        usage[dst] = tu[src]
                return usage

        # AIMessage usage_metadata
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            if isinstance(um, dict):
                for src, dst in [
                    ("input_tokens", "gen_ai.usage.input_tokens"),
                    ("output_tokens", "gen_ai.usage.output_tokens"),
                    ("total_tokens", "gen_ai.usage.total_tokens"),
                ]:
                    if src in um:
                        usage[dst] = um[src]
                return usage

        # generations[0][0].message.usage_metadata
        generations = getattr(response, "generations", None)
        if generations and len(generations) > 0 and len(generations[0]) > 0:
            gen = generations[0][0]
            msg = getattr(gen, "message", None)
            if msg:
                um = getattr(msg, "usage_metadata", None)
                if um and isinstance(um, dict):
                    for src, dst in [
                        ("input_tokens", "gen_ai.usage.input_tokens"),
                        ("output_tokens", "gen_ai.usage.output_tokens"),
                        ("total_tokens", "gen_ai.usage.total_tokens"),
                    ]:
                        if src in um:
                            usage[dst] = um[src]
                    return usage

                # response_metadata.token_usage
                rm = getattr(msg, "response_metadata", None)
                if rm and isinstance(rm, dict):
                    tu = rm.get("token_usage") or rm.get("usage")
                    if tu and isinstance(tu, dict):
                        for src, dst in [
                            ("prompt_tokens", "gen_ai.usage.input_tokens"),
                            ("completion_tokens", "gen_ai.usage.output_tokens"),
                            ("total_tokens", "gen_ai.usage.total_tokens"),
                        ]:
                            if src in tu:
                                usage[dst] = tu[src]
                        return usage
    except Exception as e:
        logger.debug("Token usage extraction failed: %s", e)
    return usage


class CallbackLLMHandle:
    """Handle for setting LLM callback span response data."""

    def __init__(self, span: Optional[Span], sampled: bool, tracer=None):
        self._span = span
        self._sampled = sampled
        self._tracer = tracer

    def set_response(self, response: Any):
        """Set the LLM response for token usage and output capture."""
        if self._span is None or not self._sampled:
            return
        try:
            usage = _extract_token_usage_from_response(response)
            for k, v in usage.items():
                self._span.set_attribute(k, v)

            strategy = self._tracer.config.payload_strategy if self._tracer else "off"
            if strategy != "off":
                try:
                    from .metadata import normalize_messages
                    messages = None
                    generations = getattr(response, "generations", None)
                    if generations and len(generations) > 0:
                        msgs = []
                        for gen_list in generations:
                            for gen in gen_list:
                                msg = getattr(gen, "message", None)
                                if msg:
                                    msgs.append(msg)
                        if msgs:
                            messages = msgs
                    if messages:
                        normalized = normalize_messages(messages)
                        masked = mask_payload(normalized, strategy)
                        guarded, truncated, orig_size = apply_size_guard(masked)
                        if self._span.payload is None:
                            self._span.payload = {}
                        self._span.payload["output"] = guarded
                except Exception as e:
                    logger.debug("LLM callback output payload failed: %s", e)
        except Exception as e:
            logger.debug("set_response failed: %s", e)

    def record_ttft(self):
        """Record Time To First Token."""
        if self._span is None or not self._sampled:
            return
        try:
            ttft_ms = round((time.time() - self._span.start_time) * 1000, 2)
            self._span.set_attribute("gen_ai.response.ttft_ms", ttft_ms)
            self._span.set_attribute("langchain.streaming", True)
        except Exception as e:
            logger.debug("TTFT recording failed: %s", e)

    def set_error(self, error_type: str, error_message: str):
        if self._span is not None:
            self._span.set_error(error_type, error_message)


class CallbackLLMSpan:
    """Context manager for a callback-driven LLM span.

    Creates an LLM child span with logical_llm_span_active=True.
    """

    def __init__(
        self,
        run_id: str,
        parent_run_id: Optional[str],
        run_type: str,
        name: str,
        serialized: Optional[dict],
        invocation_params: Optional[dict],
        config: Optional[dict],
        parent_context: SpanContext,
        tags: Optional[list] = None,
    ):
        self._run_id = run_id
        self._parent_run_id = parent_run_id
        self._run_type = run_type
        self._name = name
        self._serialized = serialized or {}
        self._invocation_params = invocation_params or {}
        self._config = config or {}
        self._parent_context = parent_context
        self._tags = tags or []
        self._span: Optional[Span] = None
        self._token = None
        self._sampled = True
        self._handle: Optional[CallbackLLMHandle] = None
        self._tracer = None

    def _get_tracer(self):
        from llm_observability import Observability
        return Observability._tracer

    def __enter__(self) -> CallbackLLMHandle:
        self._tracer = self._get_tracer()
        self._sampled = self._parent_context.sampled

        span_id = generate_span_id()
        ctx = SpanContext(
            trace_id=self._parent_context.trace_id,
            span_id=span_id,
            parent_span_id=self._parent_context.span_id,
            span_kind=SpanKind.LLM,
            sampled=self._parent_context.sampled,
            logical_llm_span_active=True,
        )

        span = Span(
            trace_id=self._parent_context.trace_id,
            span_id=span_id,
            parent_span_id=self._parent_context.span_id,
            span_name="llm.completion",
            span_kind=SpanKind.LLM,
        )

        # Attributes
        try:
            from .compat import LANGCHAIN_VERSION
            span.set_attribute("framework.name", "langchain")
            span.set_attribute("framework.version", LANGCHAIN_VERSION)
            span.set_attribute("langchain.component", "model")
            span.set_attribute("langchain.callback.mode", "true")
            span.set_attribute("langchain.run_id", self._run_id)
            if self._parent_run_id:
                span.set_attribute("langchain.parent_run_id", self._parent_run_id)
            span.set_attribute("langchain.run.name", self._name)

            model_info = _extract_model_info(self._serialized, self._invocation_params)
            for k, v in model_info.items():
                span.set_attribute(k, v)

            if self._tags:
                from .metadata import MAX_TAGS, MAX_TAG_LENGTH
                from ...utils.masking import _mask_string_patterns
                trimmed = self._tags[:MAX_TAGS]
                span.set_attribute("langchain.tags", [
                    _mask_string_patterns(str(t))[:MAX_TAG_LENGTH] for t in trimmed
                ])
        except Exception as e:
            logger.debug("LLM callback attributes failed: %s", e)

        # Input payload
        if self._tracer and self._sampled:
            strategy = self._tracer.config.payload_strategy
            if strategy != "off":
                try:
                    from .metadata import normalize_messages
                    messages = self._invocation_params.get("messages") if self._invocation_params else None
                    if messages:
                        normalized = normalize_messages(messages)
                    elif self._invocation_params.get("prompts"):
                        normalized = {"prompts": self._invocation_params["prompts"]}
                    else:
                        normalized = safe_serialize(self._invocation_params)
                    masked = mask_payload(normalized, strategy)
                    guarded, truncated, orig_size = apply_size_guard(masked)
                    if span.payload is None:
                        span.payload = {}
                    span.payload["input"] = guarded
                except Exception as e:
                    logger.debug("LLM callback input payload failed: %s", e)

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
        self._handle = CallbackLLMHandle(span, self._sampled, self._tracer)
        return self._handle

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._span is None:
            return False

        try:
            if exc_type is not None:
                from .compat import is_langgraph_interrupt
                if is_langgraph_interrupt(exc_val):
                    self._span.set_attribute("langchain.interrupted", True)
                else:
                    self._span.set_error(
                        error_type=exc_type.__name__,
                        error_message=str(exc_val) if exc_val else "",
                    )
            else:
                if self._span.status != "ERROR":
                    self._span.set_status("OK")

            self._span.end()

            if self._sampled and self._tracer:
                try:
                    self._tracer.reporter.report(self._span.to_record())
                except Exception as e:
                    logger.error("Failed to report callback LLM span: %s", e)
        finally:
            if self._token is not None:
                try:
                    reset_context(self._token)
                except Exception:
                    pass
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__exit__(exc_type, exc_val, exc_tb)
