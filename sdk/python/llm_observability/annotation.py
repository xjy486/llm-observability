"""Observability.annotate() — Phase 2.5.

Annotates the current (or explicit) span with input/output/attributes/tags/error.

Protected keys (cannot be overwritten):
    trace_id, span_id, parent_span_id, span_kind, start_time, end_time,
    duration_ms

All data passes through the privacy pipeline:
    Safe Serialize -> Sensitive Key Masking -> Pattern Masking ->
    Size Guard -> Reserved Key Protection
"""
import logging
from typing import Any, Optional

from .spans import Span
from .tool import (
    safe_serialize, apply_size_guard, mask_payload, normalize_attribute_key,
    _sanitize_attribute_pair, _apply_size_limit_to_value, MAX_ATTRIBUTE_SIZE_BYTES,
)
from .span_registry import get_span_event_sink

logger = logging.getLogger("llm_obs.annotation")

# Keys that annotate() must never overwrite
PROTECTED_KEYS = frozenset({
    "trace_id", "span_id", "parent_span_id", "span_kind",
    "start_time", "end_time", "duration_ms",
})


def _safe_error_message(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return "<error message unavailable>"


def annotate(
    span: Optional[Span],
    input_data: Any = None,
    output_data: Any = None,
    attributes: Optional[dict] = None,
    tags: Optional[list] = None,
    error: Optional[BaseException] = None,
    tracer=None,
) -> bool:
    """Annotate a span with additional data.

    Args:
        span: The Span to annotate. If None, uses the current active span
            (resolved via span_registry using the active context). If no
            active span, returns False (fail-open).
        input_data: If not None, overwrites the span's captured input.
        output_data: If not None, overwrites the span's captured output.
        attributes: dict merged into span attributes (protected keys ignored).
        tags: list saved to span attributes under 'sdk.tags'.
        error: exception object — sets ERROR status and error fields.
        tracer: required for input/output payload processing (masking strategy).

    Returns:
        True if annotation succeeded, False if no active span (fail-open).
    """
    target = span

    # Resolve current active span if none provided
    if target is None:
        from .context import get_current_context
        ctx = get_current_context()
        if ctx is None:
            return False
        sink = get_span_event_sink(ctx.trace_id, ctx.span_id)
        if sink is None:
            return False
        target = sink._span

    if not isinstance(target, Span):
        return False

    try:
        strategy = "masked"
        if tracer is not None and tracer.config is not None:
            strategy = tracer.config.payload_strategy

        # Input
        if input_data is not None and strategy != "off":
            try:
                serialized = safe_serialize(input_data)
                masked = mask_payload(serialized, strategy)
                guarded, truncated, original_size_bytes = apply_size_guard(masked)
                if target.payload is None:
                    target.payload = {}
                target.payload["input"] = guarded
                target.set_attribute("task.input.truncated", truncated)
            except Exception as e:
                logger.error("annotate input failed: %s", e)

        # Output
        if output_data is not None and strategy != "off":
            try:
                serialized = safe_serialize(output_data)
                masked = mask_payload(serialized, strategy)
                guarded, truncated, original_size_bytes = apply_size_guard(masked)
                if target.payload is None:
                    target.payload = {}
                target.payload["output"] = guarded
                target.set_attribute("task.output.truncated", truncated)
            except Exception as e:
                logger.error("annotate output failed: %s", e)

        # Attributes (merge, protect keys)
        if attributes:
            for k, v in attributes.items():
                normalized_k = normalize_attribute_key(k)
                if normalized_k in PROTECTED_KEYS:
                    logger.warning("annotate: cannot overwrite protected key '%s' — ignored", normalized_k)
                    continue
                _, sanitized = _sanitize_attribute_pair(k, v)
                sanitized = _apply_size_limit_to_value(sanitized, MAX_ATTRIBUTE_SIZE_BYTES)
                target.set_attribute(normalized_k, sanitized)

        # Tags
        if tags:
            try:
                safe_tags = safe_serialize(list(tags))
                target.set_attribute("sdk.tags", safe_tags)
            except Exception as e:
                logger.error("annotate tags failed: %s", e)

        # Error
        if error is not None:
            try:
                target.set_error(
                    error_type=type(error).__name__,
                    error_message=_safe_error_message(error),
                )
            except Exception as e:
                logger.error("annotate error failed: %s", e)

        return True
    except Exception as e:
        logger.error("annotate failed: %s", e)
        return False
