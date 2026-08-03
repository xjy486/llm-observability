"""Normalized usage data model and OpenAI-compatible normalizer (spec §8.4, §12).

UsageNormalizer maps OpenAI / OpenAI-compatible usage payloads
(``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``, plus cached /
reasoning variants when present) into ``NormalizedUsage``. Parse failures are
fail-open: the span still ends with whatever was successfully recorded.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional, Any

logger = logging.getLogger("llm_obs.gateway.usage")


@dataclass(frozen=True)
class NormalizedUsage:
    """Provider-neutral token usage (spec §8.4).

    Attributes:
        input_tokens: Prompt/input tokens.
        output_tokens: Completion/output tokens.
        total_tokens: Total tokens (if provided).
        cached_input_tokens: Cached input tokens (when present).
        reasoning_tokens: Reasoning tokens (when present).
        cache_creation_tokens: Cache-write tokens (when present).
        cache_read_tokens: Cache-read tokens (when present).
        usage_source: Where the usage came from (e.g. 'openai', 'openai-compatible').
    """
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    usage_source: Optional[str] = None


def usage_has_values(usage: Optional[NormalizedUsage]) -> bool:
    """Return True when any token field is populated."""
    if usage is None:
        return False
    return any(
        value is not None
        for value in (
            usage.input_tokens, usage.output_tokens, usage.total_tokens,
            usage.cached_input_tokens, usage.reasoning_tokens,
            usage.cache_creation_tokens, usage.cache_read_tokens,
        )
    )


def add_usage(a: Optional[NormalizedUsage], b: Optional[NormalizedUsage]) -> NormalizedUsage:
    """Add two normalized usages field-by-field (None-safe).

    The result carries no ``usage_source`` (an aggregate has no single source).
    """
    def _sum(x, y):
        if x is None:
            return y
        if y is None:
            return x
        return x + y

    return NormalizedUsage(
        input_tokens=_sum(a.input_tokens if a else None, b.input_tokens if b else None),
        output_tokens=_sum(a.output_tokens if a else None, b.output_tokens if b else None),
        total_tokens=_sum(a.total_tokens if a else None, b.total_tokens if b else None),
        cached_input_tokens=_sum(
            a.cached_input_tokens if a else None, b.cached_input_tokens if b else None
        ),
        reasoning_tokens=_sum(
            a.reasoning_tokens if a else None, b.reasoning_tokens if b else None
        ),
        cache_creation_tokens=_sum(
            a.cache_creation_tokens if a else None, b.cache_creation_tokens if b else None
        ),
        cache_read_tokens=_sum(
            a.cache_read_tokens if a else None, b.cache_read_tokens if b else None
        ),
        usage_source=None,
    )


def usage_to_attributes(usage: Optional[NormalizedUsage]) -> dict:
    """Map a NormalizedUsage onto the fixed ``usage.*`` attribute names."""
    if usage is None:
        return {}
    from .attributes import ATTR_USAGE
    result = {}
    if usage.input_tokens is not None:
        result[ATTR_USAGE["input_tokens"]] = usage.input_tokens
    if usage.output_tokens is not None:
        result[ATTR_USAGE["output_tokens"]] = usage.output_tokens
    if usage.total_tokens is not None:
        result[ATTR_USAGE["total_tokens"]] = usage.total_tokens
    if usage.cached_input_tokens is not None:
        result[ATTR_USAGE["cached_input_tokens"]] = usage.cached_input_tokens
    if usage.reasoning_tokens is not None:
        result[ATTR_USAGE["reasoning_tokens"]] = usage.reasoning_tokens
    if usage.cache_creation_tokens is not None:
        result[ATTR_USAGE["cache_creation_tokens"]] = usage.cache_creation_tokens
    if usage.cache_read_tokens is not None:
        result[ATTR_USAGE["cache_read_tokens"]] = usage.cache_read_tokens
    if usage.usage_source is not None:
        result[ATTR_USAGE["source"]] = usage.usage_source
    return result


def _as_int(value: Any) -> Optional[int]:
    """Coerce a raw usage value to int; None-safe."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class UsageNormalizer:
    """Normalize OpenAI / OpenAI-compatible usage payloads (spec §12.1).

    Round-one scope: chat-completion style usage. Anthropic Messages and
    OpenAI Responses parsing are Phase 3.1.
    """

    def __init__(self, source: Optional[str] = None):
        """Args: source — the usage_source recorded on normalized results."""
        self._source = source or "openai"

    def normalize(self, raw: Any, source: Optional[str] = None) -> Optional[NormalizedUsage]:
        """Normalize a raw usage payload; None when nothing usable is present.

        Fail-open: any parse exception is logged and returns None.
        """
        try:
            return self._normalize_inner(raw, source)
        except Exception as e:
            logger.error("Usage normalization failed: %s", e)
            return None

    def _normalize_inner(self, raw: Any, source: Optional[str]) -> Optional[NormalizedUsage]:
        if raw is None:
            return None

        # Support object-style (response.usage) and dict-style payloads.
        if isinstance(raw, dict):
            data = raw
        elif hasattr(raw, "__dict__"):
            data = {
                key: getattr(raw, key)
                for key in (
                    "prompt_tokens", "completion_tokens", "total_tokens",
                    "prompt_tokens_details", "completion_tokens_details",
                )
                if hasattr(raw, key)
            }
        else:
            return None

        usage = NormalizedUsage(
            input_tokens=_as_int(data.get("prompt_tokens")),
            output_tokens=_as_int(data.get("completion_tokens")),
            total_tokens=_as_int(data.get("total_tokens")),
            usage_source=source or self._source,
        )

        # Cached/reasoning variants when present (OpenAI-compatible detail blobs).
        prompt_details = data.get("prompt_tokens_details") or {}
        if isinstance(prompt_details, dict):
            usage = NormalizedUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=_as_int(
                    prompt_details.get("cached_tokens")
                    if isinstance(prompt_details, dict) else None
                ),
                usage_source=usage.usage_source,
            )
        completion_details = data.get("completion_tokens_details") or {}
        if isinstance(completion_details, dict):
            usage = NormalizedUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                reasoning_tokens=_as_int(
                    completion_details.get("reasoning_tokens")
                    if isinstance(completion_details, dict) else None
                ),
                usage_source=usage.usage_source,
            )

        # Cache_creation / cache_read (Anthropic-style) when present.
        cache_creation = _as_int(data.get("cache_creation_input_tokens"))
        cache_read = _as_int(data.get("cache_read_input_tokens"))
        if cache_creation is not None or cache_read is not None:
            usage = NormalizedUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cache_creation_tokens=cache_creation,
                cache_read_tokens=cache_read,
                usage_source=usage.usage_source,
            )

        if not usage_has_values(usage):
            return None
        return usage
