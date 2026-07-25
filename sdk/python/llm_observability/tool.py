"""Tool Span — lifecycle, payload, serialization, decorator.

Phase 2.2: Implements Observability.tool() and Observability.instrument_tool().
"""
import dataclasses
import functools
import inspect as _inspect
import json
import logging
from typing import Any, Optional

from .context import SpanContext, get_current_context, set_context, reset_context
from .spans import Span, SpanKind
from .utils.ids import generate_span_id
from .utils.masking import mask_payload

logger = logging.getLogger("llm_obs.tool")

DEFAULT_MAX_PAYLOAD_BYTES = 32 * 1024


def safe_serialize(value: Any) -> Any:
    """Safely serialize any Python object to a JSON-compatible representation."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (bytes, bytearray)):
        return {"_type": "bytes", "size_bytes": len(value)}

    if isinstance(value, dict):
        return {str(k): safe_serialize(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [safe_serialize(item) for item in value]

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return safe_serialize(dataclasses.asdict(value))
        except Exception:
            type_name = type(value).__name__
            try:
                safe_repr = repr(value)[:200]
            except Exception:
                safe_repr = f"<{type_name}>"
            return {"_type": type_name, "_repr": safe_repr}

    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            return safe_serialize(value.model_dump())
        except Exception:
            pass

    type_name = type(value).__name__
    try:
        safe_repr = repr(value)[:200]
    except Exception:
        safe_repr = f"<{type_name}>"
    return {"_type": type_name, "_repr": safe_repr}


def apply_size_guard(data: Any, max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> tuple:
    """Apply size guard to serialized data. Returns (guarded_data, truncated_flag)."""
    try:
        json_str = json.dumps(data, ensure_ascii=False, default=str)
        size_bytes = len(json_str.encode("utf-8"))
    except (TypeError, ValueError):
        return {"_truncated": True, "_original_size_bytes": -1, "_preview": "<unserializable>"}, True

    if size_bytes <= max_bytes:
        return data, False

    preview_len = min(512, max_bytes)
    preview = json_str[:preview_len]
    return {
        "_truncated": True,
        "_original_size_bytes": size_bytes,
        "_preview": preview,
    }, True


class ToolHandle:
    """Handle returned by Observability.tool() context manager."""

    def __init__(self, span: Span):
        self._span = span

    def set_output(self, value: Any):
        self._span._tool_output = value

    def set_attribute(self, key: str, value: Any):
        self._span.set_attribute(key, value)

    def add_event(self, name: str, attributes: dict = None):
        self._span.add_event(name, attributes=attributes)

    def set_error(self, error_type: str, error_message: str):
        self._span.set_error(error_type, error_message)


class ToolContextManager:
    """Context manager for a TOOL span."""

    def __init__(
        self,
        tracer,
        name: str,
        tool_type: Optional[str] = None,
        input: Any = None,
        call_id: Optional[str] = None,
        attributes: Optional[dict] = None,
    ):
        if not name or not name.strip():
            raise ValueError("Tool name must not be empty")

        self._tracer = tracer
        self._name = name
        self._tool_type = tool_type
        self._input = input
        self._call_id = call_id
        self._extra_attributes = attributes or {}
        self._span: Optional[Span] = None
        self._token = None
        self._handle: Optional[ToolHandle] = None

    def __enter__(self) -> ToolHandle:
        current = get_current_context()
        if current is None:
            raise RuntimeError(
                "Observability.tool() requires an active trace. "
                "Create a business trace with Observability.trace() first."
            )

        span_id = generate_span_id()
        ctx = SpanContext(
            trace_id=current.trace_id,
            span_id=span_id,
            parent_span_id=current.span_id,
            span_kind=SpanKind.TOOL,
            sampled=current.sampled,
        )
        self._token = set_context(ctx)

        self._span = Span(
            trace_id=current.trace_id,
            span_id=span_id,
            parent_span_id=current.span_id,
            span_name=f"tool.{self._name}",
            span_kind=SpanKind.TOOL,
        )

        self._span.set_attribute("tool.name", self._name)
        if self._tool_type:
            self._span.set_attribute("tool.type", self._tool_type)
        if self._call_id:
            self._span.set_attribute("tool.call_id", self._call_id)

        for k, v in self._extra_attributes.items():
            self._span.set_attribute(k, v)

        self._process_input()

        self._span.start()
        self._handle = ToolHandle(self._span)
        return self._handle

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._span.set_error(
                error_type=exc_type.__name__,
                error_message=str(exc_val),
            )
        else:
            if self._span.status != "ERROR":
                self._span.set_status("OK")

        self._process_output()
        self._set_request_metadata()
        self._span.end()

        current = get_current_context()
        if current and current.sampled:
            try:
                self._tracer.reporter.report(self._span.to_record())
            except Exception as e:
                logger.error("Failed to report TOOL span: %s", e)

        reset_context(self._token)
        return False

    def _process_input(self):
        strategy = self._tracer.config.payload_strategy
        if strategy == "off" or self._input is None:
            return

        try:
            serialized = safe_serialize(self._input)
            masked = mask_payload(serialized, strategy)
            guarded, truncated = apply_size_guard(masked)

            self._span.set_attribute("tool.input.type", type(self._input).__name__)
            try:
                size = len(json.dumps(guarded, ensure_ascii=False, default=str).encode("utf-8"))
            except Exception:
                size = 0
            self._span.set_attribute("tool.input.size_bytes", size)
            self._span.set_attribute("tool.input.truncated", truncated)

            if self._span.payload is None:
                self._span.payload = {}
            self._span.payload["input"] = guarded
        except Exception as e:
            logger.error("Tool input processing failed: %s", e)

    def _process_output(self):
        strategy = self._tracer.config.payload_strategy
        if strategy == "off":
            return

        output = getattr(self._span, "_tool_output", None)
        if output is None:
            return

        try:
            serialized = safe_serialize(output)
            masked = mask_payload(serialized, strategy)
            guarded, truncated = apply_size_guard(masked)

            self._span.set_attribute("tool.output.type", type(output).__name__)
            try:
                size = len(json.dumps(guarded, ensure_ascii=False, default=str).encode("utf-8"))
            except Exception:
                size = 0
            self._span.set_attribute("tool.output.size_bytes", size)
            self._span.set_attribute("tool.output.truncated", truncated)

            if self._span.payload is None:
                self._span.payload = {}
            self._span.payload["output"] = guarded
        except Exception as e:
            logger.error("Tool output processing failed: %s", e)

    def _set_request_metadata(self):
        self._span.request_metadata = {"tool_name": self._name}
        if self._tool_type:
            self._span.request_metadata["tool_type"] = self._tool_type
        if self._call_id:
            self._span.request_metadata["call_id"] = self._call_id

    async def __aenter__(self) -> ToolHandle:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__exit__(exc_type, exc_val, exc_tb)


def instrument_tool(tracer, name: str, tool_type: Optional[str] = None):
    """Decorator factory that wraps a function with a TOOL span."""

    def decorator(func):
        is_async = _inspect.iscoroutinefunction(func)

        if is_async:
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                bound_input = _bind_arguments(func, args, kwargs)
                with tracer.tool(
                    name=name,
                    tool_type=tool_type,
                    input=bound_input,
                ) as tool:
                    result = await func(*args, **kwargs)
                    tool.set_output(result)
                    return result
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                bound_input = _bind_arguments(func, args, kwargs)
                with tracer.tool(
                    name=name,
                    tool_type=tool_type,
                    input=bound_input,
                ) as tool:
                    result = func(*args, **kwargs)
                    tool.set_output(result)
                    return result
            return sync_wrapper

    return decorator


def _bind_arguments(func, args, kwargs) -> dict:
    """Bind function arguments to a dict, skipping self/cls."""
    try:
        sig = _inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()

        result = {}
        for param_name, value in bound.arguments.items():
            if param_name in ("self", "cls"):
                continue
            result[param_name] = value
        return result
    except Exception:
        return {"args": list(args), "kwargs": dict(kwargs)}
