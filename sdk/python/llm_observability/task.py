"""TASK Span — Phase 2.5.

Implements the TASK SpanKind used by @chain, @task, and the distributed
client_call helper. Reuses the same payload/masking/size-guard pipeline
as the TOOL span for consistency.

TASK attributes (spec §11):
    task.name
    task.type   (chain | task | client_call)
    task.call_id
    task.role   (client | server)  — only for client_call
"""
import functools
import inspect as _inspect
import logging
from typing import Any, Optional

from .context import SpanContext, get_current_context, set_context, reset_context
from .spans import Span, SpanKind
from .tool import (
    safe_serialize, apply_size_guard, mask_payload, _safe_tool_error_message,
    DEFAULT_MAX_PAYLOAD_BYTES, _OUTPUT_UNSET, normalize_attribute_key,
    _sanitize_attribute_pair, _apply_size_limit_to_value,
    RESERVED_TOOL_KEYS, MAX_ATTRIBUTE_SIZE_BYTES,
)
from .utils.ids import generate_span_id

logger = logging.getLogger("llm_obs.task")

# Reserved canonical task.* keys
RESERVED_TASK_KEYS = frozenset({
    "task.name", "task.type", "task.call_id", "task.role",
    "task.input.type", "task.output.type",
    "task.input.size_bytes", "task.output.size_bytes",
    "task.input.truncated", "task.output.truncated",
})


class TaskHandle:
    """Handle returned by the TASK span context manager."""

    def __init__(self, span: Span):
        self._span = span

    def set_output(self, value: Any):
        self._span._task_output = value

    def set_attribute(self, key: Any, value: Any):
        normalized_key = normalize_attribute_key(key)
        if normalized_key in RESERVED_TASK_KEYS:
            logger.warning("Cannot override reserved attribute '%s' — ignored", normalized_key)
            return
        _, sanitized = _sanitize_attribute_pair(key, value)
        sanitized = _apply_size_limit_to_value(sanitized, MAX_ATTRIBUTE_SIZE_BYTES)
        self._span.set_attribute(normalized_key, sanitized)

    def add_event(self, name: Any, attributes: dict = None):
        normalized_name = name
        clean_attrs = {}
        if attributes:
            for k, v in attributes.items():
                normalized_k = normalize_attribute_key(k)
                if normalized_k in RESERVED_TASK_KEYS:
                    continue
                _, sanitized = _sanitize_attribute_pair(k, v)
                clean_attrs[normalized_k] = sanitized
        self._span.add_event(normalized_name, attributes=clean_attrs)

    def set_error(self, error_type: str, error_message: str):
        self._span.set_error(error_type, error_message)


class TaskContextManager:
    """Context manager for a TASK span.

    A TASK span may be a child of AGENT, TASK, TOOL, or LLM. It requires an
    active trace (an existing SpanContext). When no context exists, behavior
    depends on `fail_open` (handled by the caller / decorator runtime).
    """

    def __init__(
        self,
        tracer,
        name: str,
        task_type: str = "task",
        input: Any = None,
        call_id: Optional[str] = None,
        role: Optional[str] = None,
        attributes: Optional[dict] = None,
    ):
        if not name or not name.strip():
            raise ValueError("Task name must not be empty")

        self._tracer = tracer
        self._name = name
        self._task_type = task_type
        self._input = input
        self._call_id = call_id
        self._role = role
        self._extra_attributes = attributes or {}
        self._span: Optional[Span] = None
        self._token = None
        self._handle: Optional[TaskHandle] = None
        self._sampled: bool = True

    def __enter__(self) -> TaskHandle:
        current = get_current_context()
        if current is None:
            raise RuntimeError(
                "TASK span requires an active trace. "
                "Create a trace with Observability.trace() or @agent first."
            )

        self._sampled = current.sampled

        span_id = generate_span_id()
        span = Span(
            trace_id=current.trace_id,
            span_id=span_id,
            parent_span_id=current.span_id,
            span_name=f"task.{self._name}",
            span_kind=SpanKind.TASK,
        )

        span.set_attribute("task.name", self._name)
        span.set_attribute("task.type", self._task_type)
        if self._call_id:
            span.set_attribute("task.call_id", self._call_id)
        if self._role:
            span.set_attribute("task.role", self._role)

        # Sanitize user-provided extra attributes
        for k, v in self._extra_attributes.items():
            normalized_k = normalize_attribute_key(k)
            if normalized_k in RESERVED_TASK_KEYS:
                logger.warning("Cannot override reserved attribute '%s' — ignored", normalized_k)
                continue
            _, sanitized = _sanitize_attribute_pair(k, v)
            sanitized = _apply_size_limit_to_value(sanitized, MAX_ATTRIBUTE_SIZE_BYTES)
            span.set_attribute(normalized_k, sanitized)

        self._span = span

        if self._sampled:
            self._process_input()

        span.start()

        # Phase 2.5: inherit association properties
        try:
            from .association import apply_association_to_span
            apply_association_to_span(span)
        except Exception:
            pass

        ctx = SpanContext(
            trace_id=current.trace_id,
            span_id=span_id,
            parent_span_id=current.span_id,
            span_kind=SpanKind.TASK,
            sampled=current.sampled,
        )

        token = None
        try:
            token = set_context(ctx)
            self._token = token
        except Exception:
            try:
                span.end()
            except Exception:
                pass
            raise

        self._handle = TaskHandle(span)
        return self._handle

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._span is None:
            if self._token is not None:
                reset_context(self._token)
            return False

        is_control_flow = False
        if exc_type is not None:
            try:
                from .integrations.langchain.compat import is_control_flow_exception
                is_control_flow = is_control_flow_exception(exc_val)
            except ImportError:
                import asyncio as _asyncio
                is_control_flow = (
                    exc_type is GeneratorExit
                    or (hasattr(_asyncio, 'CancelledError') and exc_type is _asyncio.CancelledError)
                )

        try:
            if exc_type is not None:
                if is_control_flow:
                    pass  # no ERROR for control flow
                else:
                    self._span.set_error(
                        error_type=exc_type.__name__,
                        error_message=_safe_tool_error_message(exc_val),
                    )
            else:
                if self._span.status != "ERROR":
                    self._span.set_status("OK")

            self._span.end()

            if self._sampled:
                self._process_output()
                self._set_request_metadata()

            current = get_current_context()
            if current and current.sampled:
                try:
                    self._tracer.reporter.report(self._span.to_record())
                except Exception as e:
                    logger.error("Failed to report TASK span: %s", e)
        finally:
            if self._token is not None:
                reset_context(self._token)
        return False

    async def __aenter__(self) -> TaskHandle:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__exit__(exc_type, exc_val, exc_tb)

    def _process_input(self):
        strategy = self._tracer.config.payload_strategy
        if strategy == "off" or self._input is None:
            return
        try:
            serialized = safe_serialize(self._input)
            masked = mask_payload(serialized, strategy)
            guarded, truncated, original_size_bytes = apply_size_guard(masked)
            self._span.set_attribute("task.input.type", type(self._input).__name__)
            self._span.set_attribute("task.input.size_bytes", original_size_bytes)
            self._span.set_attribute("task.input.truncated", truncated)
            if self._span.payload is None:
                self._span.payload = {}
            self._span.payload["input"] = guarded
        except Exception as e:
            logger.error("Task input processing failed: %s", e)

    def _process_output(self):
        strategy = self._tracer.config.payload_strategy
        if strategy == "off":
            return
        output = getattr(self._span, "_task_output", _OUTPUT_UNSET)
        if output is _OUTPUT_UNSET:
            return
        try:
            serialized = safe_serialize(output)
            masked = mask_payload(serialized, strategy)
            guarded, truncated, original_size_bytes = apply_size_guard(masked)
            self._span.set_attribute("task.output.type", type(output).__name__)
            self._span.set_attribute("task.output.size_bytes", original_size_bytes)
            self._span.set_attribute("task.output.truncated", truncated)
            if self._span.payload is None:
                self._span.payload = {}
            self._span.payload["output"] = guarded
        except Exception as e:
            logger.error("Task output processing failed: %s", e)

    def _set_request_metadata(self):
        self._span.request_metadata = {"task_name": self._name, "task_type": self._task_type}
        if self._call_id:
            self._span.request_metadata["call_id"] = self._call_id
        if self._role:
            self._span.request_metadata["role"] = self._role


def task(
    tracer,
    name: str,
    task_type: str = "task",
    input: Any = None,
    call_id: Optional[str] = None,
    role: Optional[str] = None,
    attributes: Optional[dict] = None,
):
    """Create a TaskContextManager (factory used by Tracer.task)."""
    return TaskContextManager(
        tracer=tracer,
        name=name,
        task_type=task_type,
        input=input,
        call_id=call_id,
        role=role,
        attributes=attributes,
    )
