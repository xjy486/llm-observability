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

# P0-1: Max length for normalized keys and event names
MAX_KEY_LENGTH = 128
MAX_EVENT_NAME_LENGTH = 128


# P1-2: Sentinel to distinguish "output never set" from "output is None"
_OUTPUT_UNSET = object()


def normalize_attribute_key(key: Any) -> str:
    """P0-1: Normalize an attribute/event key to a safe bounded string.

    Handles non-string keys (int, None, etc.) without raising AttributeError.
    Truncates to MAX_KEY_LENGTH and masks sensitive patterns.
    """
    if key is None:
        return "<empty-key>"
    try:
        value = str(key)
    except Exception:
        return "<invalid-key>"

    if not value:
        return "<empty-key>"

    if len(value) > MAX_KEY_LENGTH:
        value = value[:MAX_KEY_LENGTH]

    return value


def normalize_event_name(name: Any) -> str:
    """P0-1: Normalize an event name to a safe bounded string.

    Handles non-string names and truncates to MAX_EVENT_NAME_LENGTH.
    """
    if name is None:
        return "<empty-event-name>"
    try:
        value = str(name)
    except Exception:
        return "<invalid-event-name>"

    if not value:
        return "<empty-event-name>"

    if len(value) > MAX_EVENT_NAME_LENGTH:
        value = value[:MAX_EVENT_NAME_LENGTH]

    return value


# ── P1-6: safe_serialize with complexity protection ──

SAFE_SERIALIZE_MAX_DEPTH = 8
SAFE_SERIALIZE_MAX_ITEMS = 1000
SAFE_SERIALIZE_MAX_STRING_CHARS = 32768

# P1-3: Global complexity budget
SAFE_SERIALIZE_GLOBAL_NODE_BUDGET = 5000
SAFE_SERIALIZE_GLOBAL_CHAR_BUDGET = 65536


@dataclasses.dataclass
class SerializationBudget:
    """P1-3: Global budget for a single safe_serialize invocation.

    Tracks remaining nodes and characters across the entire serialization
    tree, preventing adversarial inputs from causing excessive processing.
    """
    remaining_nodes: int = SAFE_SERIALIZE_GLOBAL_NODE_BUDGET
    remaining_chars: int = SAFE_SERIALIZE_GLOBAL_CHAR_BUDGET

    def consume_node(self) -> bool:
        """Consume one node. Returns False if budget exhausted."""
        if self.remaining_nodes <= 0:
            return False
        self.remaining_nodes -= 1
        return True

    def consume_chars(self, n: int) -> bool:
        """Consume n characters. Returns False if budget exhausted."""
        if self.remaining_chars <= 0:
            return False
        self.remaining_chars -= n
        return True

    @property
    def exhausted(self) -> bool:
        return self.remaining_nodes <= 0 or self.remaining_chars <= 0


def safe_serialize(value: Any, _depth: int = 0, _seen: Optional[set] = None, budget: Optional["SerializationBudget"] = None) -> Any:
    """Safely serialize any Python object to a JSON-compatible representation.

    P1-6: Includes protection against:
    - Circular references (via id() tracking)
    - Excessive nesting depth (max_depth)
    - Excessive element count (max_items)
    - Excessively long strings (max_string_chars)

    P1-3: Global complexity budget prevents adversarial inputs from
    causing excessive processing across the entire serialization tree.
    """
    if _seen is None:
        _seen = set()

    # P1-3: Initialize global budget if not provided
    if budget is None:
        budget = SerializationBudget()

    # P1-3: Check global node budget
    if not budget.consume_node():
        return {"_truncated": True, "_reason": "global_budget"}

    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > SAFE_SERIALIZE_MAX_STRING_CHARS:
            budget.consume_chars(SAFE_SERIALIZE_MAX_STRING_CHARS)
            return value[:SAFE_SERIALIZE_MAX_STRING_CHARS] + "...[truncated]"
        if isinstance(value, str):
            budget.consume_chars(len(value))
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

    # P1-3: Check global budget after depth/circular checks
    if budget.exhausted:
        return {"_truncated": True, "_reason": "global_budget"}

    try:
        if isinstance(value, dict):
            result = {}
            count = 0
            for k, v in value.items():
                if count >= SAFE_SERIALIZE_MAX_ITEMS:
                    result["_truncated"] = True
                    result["_reason"] = "max_items"
                    break
                # P1-3: Check budget per item
                if budget.exhausted:
                    result["_truncated"] = True
                    result["_reason"] = "global_budget"
                    break
                result[str(k)] = safe_serialize(v, _depth + 1, _seen, budget)
                count += 1
            return result

        if isinstance(value, (list, tuple)):
            if len(value) > SAFE_SERIALIZE_MAX_ITEMS:
                truncated = list(value[:SAFE_SERIALIZE_MAX_ITEMS])
                return [
                    safe_serialize(item, _depth + 1, _seen, budget) for item in truncated
                ] + [{"_truncated": True, "_reason": "max_items"}]
            return [safe_serialize(item, _depth + 1, _seen, budget) for item in value]

        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            # P1-3 fix: Do NOT use dataclasses.asdict() which recursively copies
            # the entire dataclass BEFORE entering our budget-controlled recursion.
            # Instead, iterate fields manually so each value goes through budget checks.
            try:
                result = {}
                for field in dataclasses.fields(value):
                    if budget.exhausted:
                        result["_truncated"] = True
                        result["_reason"] = "global_budget"
                        break
                    field_value = getattr(value, field.name)
                    result[field.name] = safe_serialize(field_value, _depth + 1, _seen, budget)
                return result
            except Exception:
                type_name = type(value).__name__
                try:
                    safe_repr = repr(value)[:200]
                except Exception:
                    safe_repr = f"<{type_name}>"
                return {"_type": type_name, "_repr": safe_repr}

        if hasattr(value, "model_dump") and callable(value.model_dump):
            # P1-3: model_dump() can generate a large object before our budget
            # kicks in. We still call it (most models are small), but the result
            # is immediately subject to budget-controlled recursion.
            try:
                dumped = value.model_dump()
                return safe_serialize(dumped, _depth + 1, _seen, budget)
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


def _sanitize_attribute_pair(key: Any, value: Any) -> tuple:
    """P0-1/P0-2: Sanitize a single attribute key-value pair.

    Returns (normalized_key, sanitized_value).

    The key is normalized to a string (handles int/None/etc.).
    If the key is a sensitive key, the value is fully redacted.
    Otherwise, the value is sanitized and pattern-masked.
    """
    from .utils.masking import SENSITIVE_KEYS
    normalized_key = normalize_attribute_key(key)
    kl = normalized_key.lower()
    if kl in SENSITIVE_KEYS:
        return normalized_key, "***REDACTED***"
    return normalized_key, _sanitize_user_value(value)


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

    def set_attribute(self, key: Any, value: Any):
        # P0-1: Normalize key first (handles int/None/etc.)
        normalized_key = normalize_attribute_key(key)
        # P0-2: Protect canonical keys (check against normalized key)
        if normalized_key in RESERVED_TOOL_KEYS:
            logger.warning("Cannot override reserved attribute '%s' — ignored", normalized_key)
            return
        # P0-1/P0-2: Sanitize + mask + size-guard the value (with key context)
        _, sanitized = _sanitize_attribute_pair(key, value)
        sanitized = _apply_size_limit_to_value(sanitized, MAX_ATTRIBUTE_SIZE_BYTES)
        self._span.set_attribute(normalized_key, sanitized)

    def add_event(self, name: Any, attributes: dict = None):
        # P0-1: Normalize event name
        normalized_name = normalize_event_name(name)
        # P0-2: Sanitize event attributes
        clean_attrs = {}
        if attributes:
            for k, v in attributes.items():
                normalized_k = normalize_attribute_key(k)
                if normalized_k in RESERVED_TOOL_KEYS:
                    logger.warning("Cannot use reserved key '%s' in event — ignored", normalized_k)
                    continue
                _, sanitized = _sanitize_attribute_pair(k, v)
                clean_attrs[normalized_k] = sanitized
            # Size-guard the entire event attributes
            clean_attrs = _apply_size_limit_to_value(clean_attrs, MAX_EVENT_ATTRIBUTES_SIZE_BYTES) if isinstance(clean_attrs, dict) else clean_attrs
        self._span.add_event(normalized_name, attributes=clean_attrs)

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

        # P0-1 fix: Deferred context activation.
        # Perform ALL initialization (span creation, attribute sanitization,
        # input payload processing) BEFORE set_context(). Only activate the
        # TOOL context once everything is ready. If any step fails, the parent
        # context remains untouched — no leak.
        span_id = generate_span_id()
        span = Span(
            trace_id=current.trace_id,
            span_id=span_id,
            parent_span_id=current.span_id,
            span_name=f"tool.{self._name}",
            span_kind=SpanKind.TOOL,
        )

        span.set_attribute("tool.name", self._name)
        if self._tool_type:
            span.set_attribute("tool.type", self._tool_type)
        if self._call_id:
            span.set_attribute("tool.call_id", self._call_id)

        # P0-1/P0-2: Sanitize user-provided extra attributes (protect canonical keys)
        # Use normalized keys; bad keys won't crash here.
        for k, v in self._extra_attributes.items():
            normalized_k = normalize_attribute_key(k)
            if normalized_k in RESERVED_TOOL_KEYS:
                logger.warning("Cannot override reserved attribute '%s' — ignored", normalized_k)
                continue
            _, sanitized = _sanitize_attribute_pair(k, v)
            sanitized = _apply_size_limit_to_value(sanitized, MAX_ATTRIBUTE_SIZE_BYTES)
            span.set_attribute(normalized_k, sanitized)

        # Attach span to self early so __exit__ / cleanup can access it
        self._span = span

        # Process input BEFORE span.start() so duration is clean (P1-4)
        # P1-1: Skip payload processing if unsampled
        if self._sampled:
            self._process_input()

        span.start()

        # P0-1 fix: Activate TOOL context ONLY after all initialization succeeds.
        # If we reach this point, the span is fully initialized and ready for
        # business code. If set_context() or anything below fails, we must
        # reset the context to avoid leaking the TOOL context.
        ctx = SpanContext(
            trace_id=current.trace_id,
            span_id=span_id,
            parent_span_id=current.span_id,
            span_kind=SpanKind.TOOL,
            sampled=current.sampled,
        )

        token = None
        try:
            token = set_context(ctx)
            self._token = token
        except Exception:
            # set_context failed — end the span and don't leak anything
            try:
                span.end()
            except Exception:
                pass
            raise

        self._handle = ToolHandle(span)
        return self._handle

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._span is None:
            # __enter__ failed before span was created; nothing to clean up
            if self._token is not None:
                reset_context(self._token)
            return False

        # P1-2: Check for LangGraph interrupt (human-in-the-loop control flow)
        is_interrupt = False
        if exc_type is not None:
            try:
                from .integrations.langchain.compat import is_langgraph_interrupt
                is_interrupt = is_langgraph_interrupt(exc_val)
            except ImportError:
                pass

        if exc_type is not None:
            if is_interrupt:
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