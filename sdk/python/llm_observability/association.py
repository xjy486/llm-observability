"""Association Properties — Phase 2.5.

Provides user/session_id/message_id/business_scenario propagation via a
ContextVar. Properties are inherited by every span (AGENT/TASK/TOOL/LLM/
GATEWAY) created within the context.

Priority (spec §9):
    Span explicit value
    > Decorator explicit value
    > Association Context
    > Remote Carrier
    > None

Aliases (normalized on set):
    user_id  -> user
    business_scene -> business_scenario

Fail-closed Sanitization: masking failures return '<redacted>'.
"""
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from .utils.masking import _mask_string_patterns

logger = logging.getLogger("llm_obs.association")


# Canonical field names
CANONICAL_FIELDS = ("user", "session_id", "message_id", "business_scenario")

# Alias -> canonical
ALIASES = {
    "user_id": "user",
    "business_scene": "business_scenario",
}


@dataclass(frozen=True)
class AssociationProperties:
    """Immutable association properties container."""
    user: Optional[str] = None
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    business_scenario: Optional[str] = None

    def merge(self, other: "AssociationProperties") -> "AssociationProperties":
        """Merge: explicit (self) values win over other (fallback)."""
        return AssociationProperties(
            user=self.user if self.user is not None else other.user,
            session_id=self.session_id if self.session_id is not None else other.session_id,
            message_id=self.message_id if self.message_id is not None else other.message_id,
            business_scenario=(
                self.business_scenario if self.business_scenario is not None
                else other.business_scenario
            ),
        )

    def to_dict(self) -> dict[str, str]:
        d = {}
        if self.user is not None:
            d["user"] = self.user
        if self.session_id is not None:
            d["session_id"] = self.session_id
        if self.message_id is not None:
            d["message_id"] = self.message_id
        if self.business_scenario is not None:
            d["business_scenario"] = self.business_scenario
        return d


_ASSOCIATION_VAR: ContextVar[AssociationProperties] = ContextVar(
    "llm_obs_association", default=AssociationProperties()
)


def _sanitize_value(value: Any, max_length: int = 256) -> Optional[str]:
    """Fail-closed sanitization of an association value.

    Applies pattern masking and truncation. If masking fails, returns
    '<redacted>' to prevent sensitive data leaking through fallback.
    """
    if value is None:
        return None
    try:
        text = str(value)
        text = _mask_string_patterns(text)
        return text[:max_length]
    except Exception:
        return "<redacted>"


def _normalize_keys(props: dict[str, Any]) -> dict[str, Any]:
    """Normalize alias keys to canonical names."""
    normalized = {}
    for key, value in props.items():
        canonical = ALIASES.get(key, key)
        normalized[canonical] = value
    return normalized


# Sentinel for distinguishing "key absent" from "key explicitly None"
_UNSET = object()


def clear_association_properties() -> None:
    """P1-3: Clear association properties to an empty AssociationProperties."""
    _ASSOCIATION_VAR.set(AssociationProperties())


def get_association_properties() -> AssociationProperties:
    """Get the current association properties (never None)."""
    return _ASSOCIATION_VAR.get()


def apply_association_to_span(span) -> None:
    """Apply association properties to a span, filling gaps.

    Priority (spec §9): span explicit value > association context.
    Only fills in fields that are currently None on the span.
    """
    if span is None:
        return
    assoc = _ASSOCIATION_VAR.get()
    if assoc.user is not None and getattr(span, "user_id", None) is None:
        span.user_id = assoc.user
    if assoc.session_id is not None and getattr(span, "session_id", None) is None:
        span.session_id = assoc.session_id
    if assoc.message_id is not None and getattr(span, "message_id", None) is None:
        span.message_id = assoc.message_id
    if assoc.business_scenario is not None and getattr(span, "business_scene", None) is None:
        span.business_scene = assoc.business_scenario


def set_association_properties(props: dict[str, Any]) -> Token:
    """Set association properties, returning a token for reset.

    P1-4 (nested merge): the new properties are MERGED with the current
    context — explicit fields override, unset fields inherit the outer
    context. Normalizes aliases and applies fail-closed sanitization.
    """
    normalized = _normalize_keys(props)
    current = _ASSOCIATION_VAR.get()
    # Merge: explicit (normalized) values win; gaps inherit current context
    user = _sanitize_value(normalized.get("user")) if "user" in normalized else current.user
    session_id = _sanitize_value(normalized.get("session_id")) if "session_id" in normalized else current.session_id
    message_id = _sanitize_value(normalized.get("message_id")) if "message_id" in normalized else current.message_id
    business_scenario = (
        _sanitize_value(normalized.get("business_scenario"))
        if "business_scenario" in normalized
        else current.business_scenario
    )
    # Allow explicit None to clear a field
    if normalized.get("user", _UNSET) is None:
        user = None
    if normalized.get("session_id", _UNSET) is None:
        session_id = None
    if normalized.get("message_id", _UNSET) is None:
        message_id = None
    if normalized.get("business_scenario", _UNSET) is None:
        business_scenario = None
    sanitized = AssociationProperties(
        user=user,
        session_id=session_id,
        message_id=message_id,
        business_scenario=business_scenario,
    )
    return _ASSOCIATION_VAR.set(sanitized)


def reset_association_properties(token: Token):
    """Reset association properties to their previous value."""
    _ASSOCIATION_VAR.reset(token)


class association_context:
    """Context manager for scoped association properties.

    Usage:
        with Observability.association_context(user="alice", session_id="s1"):
            ...

    The new properties replace the current context for the duration of the
    block. On exit (including exceptions), the previous context is restored.
    """

    def __init__(self, **kwargs: Any):
        self._props = _normalize_keys(kwargs)
        self._token: Optional[Token] = None

    def __enter__(self) -> "association_context":
        self._token = set_association_properties(self._props)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._token is not None:
            reset_association_properties(self._token)
            self._token = None
        return False

    async def __aenter__(self) -> "association_context":
        self._token = set_association_properties(self._props)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._token is not None:
            reset_association_properties(self._token)
            self._token = None
        return False
