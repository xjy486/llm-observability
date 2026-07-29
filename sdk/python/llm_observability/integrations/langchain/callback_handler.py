"""LangChain Observability Callback Handler.

Hooks into LangChain's callback system to create LLM/TOOL spans and
record chain events. Inherits BaseCallbackHandler with run_inline=True
and raise_error=False.

When no active trace exists, ALL callbacks are no-op (no orphan spans).
All methods are fail-open: telemetry errors never propagate to business.
"""
import logging
import threading
import time
import re
from typing import Any, Optional, Union, Sequence

from ...context import get_current_context, reset_context, SpanContext
from ...spans import SpanKind
from .compat import BaseCallbackHandler, ensure_langchain_available
from .callback_registry import CallbackRunState, CallbackRunRegistry
from .callback_spans import CallbackLLMSpan

logger = logging.getLogger("llm_obs.integrations.langchain.callback_handler")

# Event limits (spec §12, §20)
MAX_CHAIN_EVENTS_PER_SPAN = 100
MAX_CUSTOM_EVENTS_PER_SPAN = 50
MAX_CUSTOM_EVENT_NAME_LENGTH = 128
MAX_CUSTOM_EVENT_DATA_BYTES = 8 * 1024
MAX_TEXT_EVENT_LENGTH = 2048


class LangChainObservabilityCallbackHandler(BaseCallbackHandler if BaseCallbackHandler else object):
    """Callback handler that creates observability spans from LangChain callbacks.

    Attributes:
        raise_error: False — never propagate telemetry errors.
        run_inline: True — run in current thread for ContextVar propagation.

    When no active trace exists, all callbacks are no-op.
    """

    raise_error: bool = False
    run_inline: bool = True

    def __init__(self):
        ensure_langchain_available()
        self._registry = CallbackRunRegistry()
        self._state_lock = threading.RLock()
        self._chain_event_counts: dict[str, int] = {}
        self._custom_event_counts: dict[str, int] = {}
        # Track span objects by span_id for event recording
        self._spans_by_id: dict[str, Any] = {}
        # Track the root AGENT span for chain event recording
        self._root_span: Optional[Any] = None

    def _has_active_trace(self) -> bool:
        from llm_observability import Observability
        if Observability._tracer is None:
            return False
        return get_current_context() is not None

    def _get_parent_context(self, parent_run_id: Optional[str]) -> Optional[SpanContext]:
        current = get_current_context()
        return self._registry.find_parent_context(parent_run_id or "", current)

    def _register_span(self, span_id: str, span: Any):
        with self._state_lock:
            self._spans_by_id[span_id] = span
        try:
            from ...span_registry import register_span_event_sink
            register_span_event_sink(span)
        except Exception:
            pass

    def _unregister_span(self, span: Any):
        if span is None:
            return
        try:
            from ...span_registry import unregister_span_event_sink
            unregister_span_event_sink(span.trace_id, span.span_id)
        except Exception:
            pass

    def _find_span_for_events(self, span_id: str) -> Optional[Any]:
        """Find a span or event sink to record events on."""
        with self._state_lock:
            span = self._spans_by_id.get(span_id)
        if span is not None:
            return span
        contexts = []
        current = get_current_context()
        if current is not None:
            contexts.append(current)
        contexts.extend(state.context for state in self._registry.all_runs() if state.context)
        try:
            from ...span_registry import get_span_event_sink
            for ctx in contexts:
                if ctx.span_id == span_id:
                    sink = get_span_event_sink(ctx.trace_id, span_id)
                    if sink is not None:
                        return sink
        except Exception:
            pass
        # Walk parent chain in registry.
        for state in self._registry.all_runs():
            ctx = state.context
            if ctx and ctx.span_id == span_id:
                llm_span = getattr(state, '_llm_span', None)
                if llm_span:
                    return llm_span
        return self._root_span

    # ─── Chain callbacks (virtual runs + events) ───

    def on_chain_start(
        self,
        serialized: Optional[dict],
        inputs: Any,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        tags: Optional[list] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            parent_ctx = self._get_parent_context(str(parent_run_id) if parent_run_id else None)
            if parent_ctx is None:
                return

            # If root span not yet tracked, track it
            if self._root_span is None:
                self._track_root_span()

            run_name = kwargs.get("run_name", "") or self._extract_name(serialized)
            state = CallbackRunState(
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                run_type="chain",
                name=run_name,
                context=parent_ctx,
                span=None,
                token=None,
                context_owner=False,
                virtual=True,
                sampled=parent_ctx.sampled,
                first_token_seen=False,
                started_at=time.time(),
                ended=False,
            )
            self._registry.register(state)

            self._record_chain_event("langchain.chain.start", parent_ctx.span_id, {
                "langchain.run_id": str(run_id),
                "langchain.parent_run_id": str(parent_run_id) if parent_run_id else "",
                "langchain.run.name": run_name,
                "langchain.run.type": "chain",
                "langchain.depth": self._get_depth(parent_run_id),
            })
        except Exception as e:
            logger.debug("on_chain_start failed: %s", e)

    def on_chain_end(
        self,
        outputs: Any,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            if state is None or state.ended:
                return

            state.ended = True
            parent_ctx = state.context or self._get_parent_context(str(parent_run_id) if parent_run_id else None)

            self._record_chain_event("langchain.chain.end", parent_ctx.span_id if parent_ctx else "", {
                "langchain.run_id": str(run_id),
                "langchain.status": "ok",
                "duration_ms": round((time.time() - state.started_at) * 1000, 2) if state.started_at else 0,
            })
            self._registry.remove(str(run_id))
        except Exception as e:
            logger.debug("on_chain_end failed: %s", e)

    def on_chain_error(
        self,
        error: BaseException,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            if state is None or state.ended:
                return

            state.ended = True
            parent_ctx = state.context or self._get_parent_context(str(parent_run_id) if parent_run_id else None)

            self._record_chain_event("langchain.chain.error", parent_ctx.span_id if parent_ctx else "", {
                "langchain.run_id": str(run_id),
                "langchain.status": "error",
                "langchain.error.type": type(error).__name__,
            })
            self._registry.remove(str(run_id))
        except Exception as e:
            logger.debug("on_chain_error failed: %s", e)

    # ─── LLM / Chat Model callbacks ───

    def on_chat_model_start(
        self,
        serialized: Optional[dict],
        messages: list,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        tags: Optional[list] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ):
        try:
            invocation_params = dict(kwargs.get("invocation_params", {}) or {})
            invocation_params["messages"] = messages
            run_name = kwargs.get("run_name", "") or self._extract_name(serialized)
            self._start_llm_span(
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                run_type="chat_model",
                name=run_name,
                serialized=serialized,
                invocation_params=invocation_params or {},
                tags=tags or [],
            )
        except Exception as e:
            logger.debug("on_chat_model_start failed: %s", e)

    def on_llm_start(
        self,
        serialized: Optional[dict],
        prompts: list,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        tags: Optional[list] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ):
        try:
            invocation_params = kwargs.get("invocation_params", {}) or {}
            params = dict(invocation_params)
            params["prompts"] = prompts
            run_name = kwargs.get("run_name", "") or self._extract_name(serialized)
            self._start_llm_span(
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                run_type="llm",
                name=run_name,
                serialized=serialized,
                invocation_params=params,
                tags=tags or [],
            )
        except Exception as e:
            logger.debug("on_llm_start failed: %s", e)

    def on_llm_new_token(
        self,
        token: str,
        chunk: Optional[Any] = None,
        run_id: Any = None,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace() or run_id is None:
                return

            state = self._registry.get(str(run_id))
            if state is None or state.first_token_seen:
                return

            # Check for non-empty token content
            token_content = token
            if not token_content and chunk:
                # Try to extract content from chunk
                try:
                    if hasattr(chunk, "choices") and chunk.choices:
                        delta = chunk.choices[0].delta if hasattr(chunk.choices[0], "delta") else None
                        if delta and hasattr(delta, "content") and delta.content:
                            token_content = delta.content
                except Exception:
                    pass

            if token_content and str(token_content).strip():
                state.first_token_seen = True
                span = getattr(state, '_llm_span', None)
                if span is not None:
                    ttft_ms = round((time.time() - span.start_time) * 1000, 2)
                    span.set_attribute("gen_ai.response.ttft_ms", ttft_ms)
                    span.set_attribute("langchain.streaming", True)
        except Exception as e:
            logger.debug("on_llm_new_token failed: %s", e)

    def on_llm_end(
        self,
        response: Any,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            if state is None or state.ended:
                return

            state.ended = True
            handle = getattr(state, '_llm_handle', None)
            if handle:
                handle.set_response(response)

            self._finalize_llm_span(state, exc_type=None, exc_val=None)
            self._registry.remove(str(run_id))
        except Exception as e:
            logger.debug("on_llm_end failed: %s", e)

    def on_llm_error(
        self,
        error: BaseException,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            if state is None or state.ended:
                return

            state.ended = True
            self._finalize_llm_span(state, exc_type=type(error), exc_val=error)
            self._registry.remove(str(run_id))
        except Exception as e:
            logger.debug("on_llm_error failed: %s", e)

    # ─── Tool callbacks ───

    def on_tool_start(
        self,
        serialized: Optional[dict],
        input_str: str,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        tags: Optional[list] = None,
        metadata: Optional[dict] = None,
        inputs: Optional[dict] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            parent_ctx = self._get_parent_context(str(parent_run_id) if parent_run_id else None)
            if parent_ctx is None:
                return

            from llm_observability import Observability
            tracer = Observability._tracer
            if tracer is None:
                return

            current = get_current_context()
            if current and current.span_kind == SpanKind.TOOL:
                state = CallbackRunState(
                    run_id=str(run_id),
                    parent_run_id=str(parent_run_id) if parent_run_id else None,
                    run_type="tool",
                    name=kwargs.get("run_name", "") or self._extract_name(serialized) or "tool",
                    context=current,
                    span=None,
                    token=None,
                    context_owner=False,
                    virtual=True,
                    sampled=current.sampled,
                    first_token_seen=False,
                    started_at=time.time(),
                    ended=False,
                )
                self._registry.register(state)
                return

            run_name = kwargs.get("run_name", "") or self._extract_name(serialized) or "tool"
            tool_input = inputs if inputs else input_str

            call_id = (
                kwargs.get("call_id") or kwargs.get("tool_call_id")
                or (metadata or {}).get("call_id")
            )
            from .compat import LANGCHAIN_VERSION
            tool_cm = tracer.tool(
                name=run_name,
                tool_type="langchain",
                input=tool_input,
                call_id=str(call_id) if call_id else None,
                attributes={
                    "framework.name": "langchain",
                    "framework.version": LANGCHAIN_VERSION,
                    "langchain.component": "tool",
                    "langchain.callback.mode": "true",
                    "langchain.run_id": str(run_id),
                    "langchain.parent_run_id": str(parent_run_id) if parent_run_id else "",
                },
            )
            handle = tool_cm.__enter__()

            # Register the tool span for event recording
            if hasattr(tool_cm, '_span') and tool_cm._span:
                self._register_span(tool_cm._span.span_id, tool_cm._span)

            from ...context import get_current_context
            current = get_current_context()
            state = CallbackRunState(
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                run_type="tool",
                name=run_name,
                context=current,
                span=tool_cm,
                token=None,
                context_owner=True,
                virtual=False,
                sampled=parent_ctx.sampled,
                first_token_seen=False,
                started_at=time.time(),
                ended=False,
            )
            state._tool_handle = handle  # type: ignore
            self._registry.register(state)
        except Exception as e:
            logger.debug("on_tool_start failed: %s", e)

    def on_tool_end(
        self,
        output: Any,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            if state is None or state.ended:
                return

            state.ended = True
            handle = getattr(state, '_tool_handle', None)
            if handle:
                try:
                    handle.set_output(output)
                except Exception:
                    pass

            tool_cm = state.span
            if tool_cm is not None:
                tool_cm.__exit__(None, None, None)

            self._registry.remove(str(run_id))
        except Exception as e:
            logger.debug("on_tool_end failed: %s", e)

    def on_tool_error(
        self,
        error: BaseException,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            if state is None or state.ended:
                return

            state.ended = True
            tool_cm = state.span
            if tool_cm is not None:
                tool_cm.__exit__(type(error), error, error.__traceback__)

            self._registry.remove(str(run_id))
        except Exception as e:
            logger.debug("on_tool_error failed: %s", e)

    # ─── Retriever callbacks ───

    def on_retriever_start(
        self,
        serialized: Optional[dict],
        query: str,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        tags: Optional[list] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            parent_ctx = self._get_parent_context(str(parent_run_id) if parent_run_id else None)
            if parent_ctx is None:
                return

            from llm_observability import Observability
            tracer = Observability._tracer
            if tracer is None:
                return

            current = get_current_context()
            if current and current.span_kind == SpanKind.TOOL:
                state = CallbackRunState(
                    run_id=str(run_id),
                    parent_run_id=str(parent_run_id) if parent_run_id else None,
                    run_type="retriever",
                    name=kwargs.get("run_name", "") or self._extract_name(serialized) or "retriever",
                    context=current,
                    span=None,
                    token=None,
                    context_owner=False,
                    virtual=True,
                    sampled=current.sampled,
                    first_token_seen=False,
                    started_at=time.time(),
                    ended=False,
                )
                self._registry.register(state)
                return

            run_name = kwargs.get("run_name", "") or self._extract_name(serialized) or "retriever"

            call_id = (kwargs.get("call_id") or kwargs.get("retriever_call_id")
                       or (metadata or {}).get("call_id"))
            from .compat import LANGCHAIN_VERSION
            tool_cm = tracer.tool(
                name=run_name,
                tool_type="retriever",
                input={"query": query},
                call_id=str(call_id) if call_id else None,
                attributes={
                    "framework.name": "langchain",
                    "framework.version": LANGCHAIN_VERSION,
                    "langchain.component": "retriever",
                    "langchain.callback.mode": "true",
                    "langchain.run_id": str(run_id),
                    "langchain.parent_run_id": str(parent_run_id) if parent_run_id else "",
                },
            )
            handle = tool_cm.__enter__()

            if hasattr(tool_cm, '_span') and tool_cm._span:
                self._register_span(tool_cm._span.span_id, tool_cm._span)

            from ...context import get_current_context
            current = get_current_context()
            state = CallbackRunState(
                run_id=str(run_id),
                parent_run_id=str(parent_run_id) if parent_run_id else None,
                run_type="retriever",
                name=run_name,
                context=current,
                span=tool_cm,
                token=None,
                context_owner=True,
                virtual=False,
                sampled=parent_ctx.sampled,
                first_token_seen=False,
                started_at=time.time(),
                ended=False,
            )
            state._tool_handle = handle  # type: ignore
            self._registry.register(state)
        except Exception as e:
            logger.debug("on_retriever_start failed: %s", e)

    def on_retriever_end(
        self,
        documents: Sequence,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            if state is None or state.ended:
                return

            state.ended = True
            handle = getattr(state, '_tool_handle', None)

            # Record retriever metadata; document bodies are opt-in.
            if handle and documents:
                try:
                    doc_count = len(documents)
                    handle.set_attribute("retriever.document_count", doc_count)
                    metadata_keys = set()
                    total_chars = 0
                    contents = []
                    for doc in documents:
                        meta = getattr(doc, "metadata", {}) or {}
                        metadata_keys.update(meta.keys())
                        content = getattr(doc, "page_content", "") or ""
                        total_chars += len(content)
                        contents.append(content)
                    handle.set_attribute("retriever.document_metadata_keys", list(metadata_keys)[:20])
                    handle.set_attribute("retriever.total_content_chars", total_chars)
                    tracer = getattr(__import__("llm_observability", fromlist=["Observability"]), "Observability")._tracer
                    if getattr(getattr(tracer, "config", None), "capture_retriever_content", False):
                        from ...tool import safe_serialize, apply_size_guard
                        from ...utils.masking import mask_payload
                        strategy = getattr(tracer.config, "payload_strategy", "masked")
                        content = mask_payload(safe_serialize(contents), strategy)
                        guarded, truncated, _ = apply_size_guard(content)
                        if hasattr(handle, "set_output"):
                            handle.set_output({"documents": guarded, "truncated": truncated})
                except Exception:
                    pass

            tool_cm = state.span
            if tool_cm is not None:
                tool_cm.__exit__(None, None, None)

            self._registry.remove(str(run_id))
        except Exception as e:
            logger.debug("on_retriever_end failed: %s", e)

    def on_retriever_error(
        self,
        error: BaseException,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            if state is None or state.ended:
                return

            state.ended = True
            tool_cm = state.span
            if tool_cm is not None:
                tool_cm.__exit__(type(error), error, error.__traceback__)

            self._registry.remove(str(run_id))
        except Exception as e:
            logger.debug("on_retriever_error failed: %s", e)

    # ─── Retry callback ───

    def on_retry(
        self,
        retry_state: Any,
        run_id: Any,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            parent_ctx = self._get_parent_context(str(parent_run_id) if parent_run_id else None)
            if parent_ctx is None:
                return

            attrs = {
                "langchain.run_id": str(run_id),
                "langchain.parent_run_id": str(parent_run_id) if parent_run_id else "",
            }
            try:
                attrs["attempt_number"] = getattr(retry_state, "attempt_number", 0)
                outcome = getattr(retry_state, "outcome", None)
                if outcome and hasattr(outcome, "exception"):
                    e = outcome.exception()
                    if e:
                        attrs["exception_type"] = type(e).__name__
                next_action = getattr(retry_state, "next_action", None)
                if next_action and hasattr(next_action, "sleep"):
                    attrs["next_sleep_ms"] = round(next_action.sleep * 1000, 2)
            except Exception:
                pass

            self._record_event_on_span("langchain.retry", parent_ctx, attrs)
        except Exception as e:
            logger.debug("on_retry failed: %s", e)

    # ─── Custom event ───

    def on_custom_event(
        self,
        name: str,
        data: Any,
        run_id: Any,
        tags: Optional[list] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ):
        try:
            if not self._has_active_trace():
                return

            state = self._registry.get(str(run_id))
            parent_ctx = state.context if state else get_current_context()
            if parent_ctx is None:
                return

            normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(name)).strip("-.")
            event_name = f"langchain.custom.{normalized[:MAX_CUSTOM_EVENT_NAME_LENGTH - 17]}"
            with self._state_lock:
                count = self._custom_event_counts.get(parent_ctx.span_id, 0)
                if count >= MAX_CUSTOM_EVENTS_PER_SPAN:
                    logger.debug("Custom event limit reached for span %s", parent_ctx.span_id)
                    return
                self._custom_event_counts[parent_ctx.span_id] = count + 1

            from llm_observability import Observability
            strategy = getattr(getattr(Observability._tracer, "config", None), "payload_strategy", "masked")
            if strategy == "off":
                return
            from ...tool import safe_serialize, apply_size_guard
            from ...utils.masking import mask_payload
            serialized = safe_serialize(data)
            masked = mask_payload(serialized, strategy)
            guarded, truncated, orig_size = apply_size_guard(masked, max_bytes=MAX_CUSTOM_EVENT_DATA_BYTES)

            self._record_event_on_span(event_name, parent_ctx, {
                "langchain.data": guarded,
                "langchain.data.truncated": truncated,
                "langchain.data.size_bytes": orig_size,
            })
        except Exception as e:
            logger.debug("on_custom_event failed: %s", e)

    # ─── Text callback (disabled by default) ───

    def on_text(
        self,
        text: str,
        run_id: Any = None,
        parent_run_id: Optional[Any] = None,
        **kwargs,
    ):
        pass

    # ─── Cleanup ───

    def close_open_runs(self, reason: str = "wrapper_exit"):
        """Best-effort cleanup of unfinished runs."""
        try:
            for state in self._registry.all_runs():
                if state.ended:
                    continue
                try:
                    span = getattr(state, '_llm_span', None)
                    if span:
                        span.set_attribute("langchain.callback.incomplete", True)

                    # Close LLM spans
                    if state.token is not None:
                        try:
                            reset_context(state.token)
                        except Exception:
                            pass
                    if span:
                        span.end()
                        from llm_observability import Observability
                        tracer = Observability._tracer
                        if state.sampled and tracer:
                            try:
                                tracer.reporter.report(span.to_record())
                            except Exception:
                                pass

                    # Close tool spans
                    tool_cm = state.span if state.run_type in ("tool", "retriever") else None
                    if tool_cm is not None and hasattr(tool_cm, '__exit__'):
                        tool_cm.__exit__(None, None, None)
                except Exception:
                    pass
            self._registry.clear()
            with self._state_lock:
                self._chain_event_counts.clear()
                self._custom_event_counts.clear()
                self._spans_by_id.clear()
            self._root_span = None
        except Exception as e:
            logger.debug("close_open_runs failed: %s", e)

    # ─── Internal helpers ───

    def _track_root_span(self):
        """Track the root AGENT span for chain event recording."""
        try:
            from llm_observability import Observability
            # The root span is managed by TraceContextManager
            # We can't directly access it, but we can use the current context
            ctx = get_current_context()
            if ctx is None:
                return
            # We'll record events on the root span by finding it via the tracer
            # The simplest approach: store a reference when we see the AGENT context
            # For now, we use _find_span_for_events which falls back to _root_span
        except Exception:
            pass

    def _start_llm_span(
        self,
        run_id: str,
        parent_run_id: Optional[str],
        run_type: str,
        name: str,
        serialized: Optional[dict],
        invocation_params: dict,
        tags: list,
    ):
        """Start an LLM span from callback parameters."""
        try:
            if not self._has_active_trace():
                return

            parent_ctx = self._get_parent_context(parent_run_id)
            if parent_ctx is None:
                return

            # Check dedup: if already in an LLM context, register virtual (spec §17)
            from ...context import get_current_context
            current = get_current_context()
            if current and (current.logical_llm_span_active or current.span_kind == SpanKind.LLM):
                state = CallbackRunState(
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    run_type=run_type,
                    name=name,
                    context=current,
                    span=None,
                    token=None,
                    context_owner=False,
                    virtual=True,
                    sampled=current.sampled,
                    first_token_seen=False,
                    started_at=time.time(),
                    ended=False,
                )
                self._registry.register(state)
                return

            llm_span = CallbackLLMSpan(
                run_id=run_id,
                parent_run_id=parent_run_id,
                run_type=run_type,
                name=name,
                serialized=serialized,
                invocation_params=invocation_params,
                config={},
                parent_context=parent_ctx,
                tags=tags,
            )
            handle = llm_span.__enter__()

            from ...context import get_current_context
            current_after = get_current_context()
            state = CallbackRunState(
                run_id=run_id,
                parent_run_id=parent_run_id,
                run_type=run_type,
                name=name,
                context=current_after,
                span=None,
                token=llm_span._token,
                context_owner=True,
                virtual=False,
                sampled=parent_ctx.sampled,
                first_token_seen=False,
                started_at=time.time(),
                ended=False,
            )
            state._llm_handle = handle  # type: ignore
            state._llm_span = llm_span._span  # type: ignore
            state._llm_cm = llm_span  # type: ignore
            self._registry.register(state)

            # Register span for event recording
            if llm_span._span:
                self._register_span(llm_span._span.span_id, llm_span._span)
        except Exception as e:
            logger.debug("_start_llm_span failed: %s", e)

    def _finalize_llm_span(self, state: CallbackRunState, exc_type=None, exc_val=None):
        """Finalize an LLM span while always restoring callback context."""
        try:
            try:
                span = getattr(state, "_llm_span", None)
                if span is None:
                    return
                if exc_type is not None:
                    from .compat import is_langgraph_interrupt
                    if is_langgraph_interrupt(exc_val):
                        span.set_attribute("langchain.interrupted", True)
                    else:
                        span.set_error(
                            error_type=exc_type.__name__,
                            error_message=_safe_callback_error_message(exc_val),
                        )
                elif span.status != "ERROR":
                    span.set_status("OK")
                span.end()
                self._unregister_span(span)
                from llm_observability import Observability
                tracer = Observability._tracer
                if state.sampled and tracer:
                    tracer.reporter.report(span.to_record())
            except Exception:
                logger.exception("Callback LLM finalization failed")
        finally:
            if state.token is not None:
                try:
                    reset_context(state.token)
                except Exception:
                    logger.debug("Callback LLM context reset failed", exc_info=True)


    def _extract_name(self, serialized: Optional[dict]) -> str:
        try:
            if not serialized:
                return ""
            name = serialized.get("name", "")
            if name:
                return str(name)
            ids = serialized.get("id", [])
            if ids:
                return str(ids[-1])
        except Exception:
            pass
        return ""

    def _get_depth(self, parent_run_id: Optional[Any]) -> int:
        depth = 0
        rid = str(parent_run_id) if parent_run_id else None
        visited = set()
        while rid and rid not in visited:
            visited.add(rid)
            state = self._registry.get(rid)
            if state is None:
                break
            depth += 1
            rid = state.parent_run_id
        return depth

    def _record_chain_event(
        self,
        event_name: str,
        parent_span_id: str,
        attributes: dict,
    ):
        """Record a chain event on the nearest real parent span."""
        try:
            if not parent_span_id:
                return

            with self._state_lock:
                count = self._chain_event_counts.get(parent_span_id, 0)
                if count >= MAX_CHAIN_EVENTS_PER_SPAN:
                    self._chain_event_counts[parent_span_id] = count + 1
                    span = self._find_span_for_events(parent_span_id)
                    if span:
                        span.set_attribute("langchain.events.truncated", True)
                        span.set_attribute("langchain.events.dropped_count", count - MAX_CHAIN_EVENTS_PER_SPAN + 1)
                    return
                self._chain_event_counts[parent_span_id] = count + 1

            span = self._find_span_for_events(parent_span_id)
            if span is not None:
                span.add_event(event_name, time.time(), attributes)
        except Exception as e:
            logger.debug("_record_chain_event failed: %s", e)

    def _record_event_on_span(
        self,
        event_name: str,
        parent_ctx: Optional[SpanContext],
        attributes: dict,
    ):
        try:
            if parent_ctx is None:
                return
            span = self._find_span_for_events(parent_ctx.span_id)
            if span is not None:
                span.add_event(event_name, time.time(), attributes)
        except Exception as e:
            logger.debug("_record_event_on_span failed: %s", e)


def _safe_callback_error_message(error: BaseException) -> str:
    try:
        return str(error)
    except Exception:
        return "<error message unavailable>"
