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

# P0-2: Canonical tool.* keys that users cannot override via attributes/events.
RESERVED_TOOL_KEYS = frozenset({
    "tool.name", "tool.type", "tool.call_id",
    "tool.input.type", "tool.output.type",
    "tool.input.size_bytes", "tool.output.size_bytes",
    "tool.input.truncated", "tool.output.truncated",
})

# P0-2: Size limits for user-provided attributes and events
MAX_ATTRIBUTE_SIZE_BYTES = 4 * 1024  # 4 KiB per attribute
MAX_EVENT_ATTRIBUTES_SIZE_BYTES = 16 * 1024  # 16 KiB per event attributes


# P1-2: Sentinel to distinguish "output never set" from "output is None"
_OUTPUT_UNSET = object()


# ── P1-6: safe_serialize with complexity protection ──

SAFE_SERIALIZE_MAX_DEPTH = 8
SAFE_SERIALIZE_MAX_ITEMS = 1000
SAFE_SERIALIZE_MAX_STRING_CHARS = 32768


def safe_serialize(value: Any, _depth: int = 0, _seen: Optional[set] = None) -> Any:
    """Safely serialize any Python object to a JSON-compatible representation.

    P1-6: Includes protection against:
    - Circular references (via id() tracking)
    - Excessive nesting depth (max_depth)
    - Excessive element count (max_items)
    - Excessively long strings (max_string_chars)
    """
    if _seen is None:
        _seen = set()

    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > SAFE_SERIALIZE_MAX_STRING_CHARS:
            return value[:SAFE_SERIALIZE_MAX_STRING_CHARS] + "...[truncated]"
        return value

    if isinstance(value, (bytes, bytearray)):
        return {"_type": "bytes", "size_bytes": len(value)}

    # P1-6: Circular reference detection
    obj_id = id(value)
    if obj_id in _seen:
        return {"_type": "circular_reference"}
    _seen.add(obj_id)

    # P1-6: Max depth protection
    if _depth >= SAFE_SERIALIZE_MAX_DEPTH:
        return {"_truncated": True, "_reason": "max_depth"}

    try:
        if isinstance(value, dict):
            result = {}
            count = 0
            for k, v in value.items():
                if count >= SAFE_SERIALIZE_MAX_ITEMS:
                    result["_truncated"] = True
                    result["_reason"] = "max_items"
                    break
                result[str(k)] = safe_serialize(v, _depth + 1, _seen)
                count += 1
            return result

        if isinstance(value, (list, tuple)):
            if len(value) > SAFE_SERIALIZE_MAX_ITEMS:
                truncated = list(value[:SAFE_SERIALIZE_MAX_ITEMS])
                return [
                    safe_serialize(item, _depth + 1, _seen) for item in truncated
                ] + [{"_truncated": True, "_reason": "max_items"}]
            return [safe_serialize(item, _depth + 1, _seen) for item in value]

        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            try:
                return safe_serialize(dataclasses.asdict(value), _depth + 1, _seen)
            except Exception:
                type_name = type(value).__name__
                try:
                    safe_repr = repr(value)[:200]
                except Exception:
                    safe_repr = f"<{type_name}>"
                return {"_type": type_name, "_repr": safe_repr}

        if hasattr(value, "model_dump") and callable(value.model_dump):
            try:
                return safe_serialize(value.model_dump(), _depth + 1, _seen)
            except Exception:
                pass

        type_name = type(value).__name__
        try:
            safe_repr = repr(value)[:200]
        except Exception:
            safe_repr = f"<{type_name}>"
        return {"_type": type_name, "_repr": safe_repr}
    finally:
        _seen.discard(obj_id)


def apply_size_guard(data: Any, max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> tuple:
    """Apply size guard to serialized data.

    P1-3 fix: Returns (guarded_data, truncated, original_size_bytes).
    original_size_bytes is the size BEFORE truncation (after masking).
    """
    try:
        json_str = json.dumps(data, ensure_ascii=False, default=str)
        original_size_bytes = len(json_str.encode("utf-8"))
    except (TypeError, ValueError):
        return {"_truncated": True, "_original_size_bytes": -1, "_preview": "<unserializable>"}, True, -1

    if original_size_bytes <= max_bytes:
        return data, False, original_size_bytes

    preview_len = min(512, max_bytes)
    preview = json_str[:preview_len]
    return {
        "_truncated": True,
        "_original_size_bytes": original_size_bytes,
        "_preview": preview,
    }, True, original_size_bytes


def _sanitize_user_value(value: Any) -> Any:
    """P0-2: Sanitize a user-provided attribute/event value.

    Ensures the value is:
    1. JSON-safe (via safe_serialize)
    2. Masked (sensitive patterns redacted in strings)
    3. Size-bounded
    """
    from .utils.masking import mask_payload, _mask_string_patterns
    try:
        serialized = safe_serialize(value)
        # Apply masking: for dict/list this masks sensitive keys;
        # for strings this applies regex pattern masking
        masked = mask_payload(serialized, "masked")
        # Verify JSON-encodability
        json.dumps(masked)
        return masked
    except Exception:
        return "<unserializable>"


def _sanitize_attribute_pair(key: str, value: Any) -> Any:
    """P0-2: Sanitize a single attribute key-value pair.

    If the key itself is a sensitive key, the value is fully redacted.
    Otherwise, the value is sanitized and pattern-masked.
    """
    from .utils.masking import SENSITIVE_KEYS
    kl = key.lower()
    if kl in SENSITIVE_KEYS:
        return "***REDACTED***"
    return _sanitize_user_value(value)


def _apply_size_limit_to_value(value: Any, max_bytes: int) -> Any:
    """P0-2: Apply a size limit to a single value, truncating if necessary."""
    try:
        json_str = json.dumps(value, ensure_ascii=False, default=str)
        size = len(json_str.encode("utf-8"))
        if size <= max_bytes:
            return value
        # Truncate: return a summary
        return {"_truncated": True, "_original_size_bytes": size, "_preview": json_str[:512]}
    except Exception:
        return "<unserializable>"


class ToolHandle:
    """Handle returned by Observability.tool() context manager."""

    def __init__(self, span: Span):
        self._span = span

    def set_output(self, value: Any):
        # P1-2: Use sentinel — even None is a valid output
        self._span._tool_output = value

    def set_attribute(self, key: str, value: Any):
        # P0-2: Protect canonical keys
        if key in RESERVED_TOOL_KEYS:
            logger.warning("Cannot override reserved attribute '%s' — ignored", key)
            return
        # P0-2: Sanitize + mask + size-guard the value (with key context)
        sanitized = _sanitize_attribute_pair(key, value)
        sanitized = _apply_size_limit_to_value(sanitized, MAX_ATTRIBUTE_SIZE_BYTES)
        self._span.set_attribute(key, sanitized)

    def add_event(self, name: str, attributes: dict = None):
        # P0-2: Sanitize event attributes
        clean_attrs = {}
        if attributes:
            for k, v in attributes.items():
                if k in RESERVED_TOOL_KEYS:
                    logger.warning("Cannot use reserved key '%s' in event — ignored", k)
                    continue
                sanitized = _sanitize_attribute_pair(k, v)
                clean_attrs[k] = sanitized
            # Size-guard the entire event attributes
            clean_attrs = _apply_size_limit_to_value(clean_attrs, MAX_EVENT_ATTRIBUTES_SIZE_BYTES) if isinstance(clean_attrs, dict) else clean_attrs
        self._span.add_event(name, attributes=clean_attrs)

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
        self._sampled: bool = True

    def __enter__(self) -> ToolHandle:
        current = get_current_context()
        if current is None:
            raise RuntimeError(
                "Observability.tool() requires an active trace. "
                "Create a business trace with Observability.trace() first."
            )

        self._sampled = current.sampled

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

        # P0-2: Sanitize user-provided extra attributes (protect canonical keys)
        for k, v in self._extra_attributes.items():
            if k in RESERVED_TOOL_KEYS:
                logger.warning("Cannot override reserved attribute '%s' — ignored", k)
                continue
            sanitized = _sanitize_attribute_pair(k, v)
            sanitized = _apply_size_limit_to_value(sanitized, MAX_ATTRIBUTE_SIZE_BYTES)
            self._span.set_attribute(k, sanitized)

        # P0-2: Process input BEFORE span.start() so duration is clean (P1-4)
        # P1-1: Skip payload processing if unsampled
        if self._sampled:
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

        # P1-4: End the span BEFORE processing output telemetry,
        # so duration_ms only measures business execution time.
        self._span.end()

        # P1-1: Skip payload processing if unsampled
        # P1-4: Output processing happens AFTER span.end()
        if self._sampled:
            self._process_output()
            self._set_request_metadata()

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
            # P1-3: apply_size_guard now returns original_size_bytes
            guarded, truncated, original_size_bytes = apply_size_guard(masked)

            self._span.set_attribute("tool.input.type", type(self._input).__name__)
            # P1-3: size_bytes = original serialized size (after masking, before truncation)
            self._span.set_attribute("tool.input.size_bytes", original_size_bytes)
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

        # P1-2: Use sentinel to distinguish "never set" from "set to None"
        output = getattr(self._span, "_tool_output", _OUTPUT_UNSET)
        if output is _OUTPUT_UNSET:
            return  # set_output() was never called

        try:
            serialized = safe_serialize(output)
            masked = mask_payload(serialized, strategy)
            # P1-3: apply_size_guard now returns original_size_bytes
            guarded, truncated, original_size_bytes = apply_size_guard(masked)

            self._span.set_attribute("tool.output.type", type(output).__name__)
            # P1-3: size_bytes = original serialized size
            self._span.set_attribute("tool.output.size_bytes", original_size_bytes)
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


# ── Backward-compat: instrument_tool for Tracer.instrument_tool() ──
# P0-1: The public Observability.instrument_tool() now handles lazy init itself.
# This function remains for Tracer.instrument_tool() (used after init).

def instrument_tool(tracer, name: str, tool_type: Optional[str] = None):
    """Decorator factory that wraps a function with a TOOL span.

    P0-1: This is the Tracer-level decorator (used after SDK init).
    The public Observability.instrument_tool() has its own lazy-init wrapper.
    """

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