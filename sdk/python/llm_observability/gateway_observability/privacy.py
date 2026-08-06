"""PrivacyGuard — default-deny secret protection (spec §16, runtime spec).

Blocks by default the recording of Authorization headers, API keys, cookies,
set-cookie, raw channel secrets, full upstream URLs, full prompts/responses,
tool I/O, and uploaded files. Allows provider names, hashed channel IDs, model
names, HTTP status, Usage, Cost, error categories, and request IDs. Channel
IDs are hashed/HMAC'd from internal raw IDs by default.

Masking is fail-closed: a masking failure yields ``<redacted>`` and the span
still proceeds.
"""
import hashlib
import hmac
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("llm_obs.gateway.privacy")

REDACTED: str = "<redacted>"

# Secret patterns in string values (mask to <redacted>).
_SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9]{16,}", re.IGNORECASE), REDACTED),
    (re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.IGNORECASE), "Bearer " + REDACTED),
    (re.compile(r"api[_-]?key\s*[=:]\s*\S+", re.IGNORECASE), REDACTED),
    (re.compile(r"(?:password|passwd|secret|token)\s*[=:]\s*\S+", re.IGNORECASE), REDACTED),
]

# Sensitive-key prefix signals — any attribute key starting with one of these
# is default-deny, even if it would otherwise match an allowed leaf.
_DENIED_KEY_PREFIXES = (
    "authorization", "proxy-authorization", "x-auth-token", "x-api-key",
    "api_key", "apikey", "api-key", "openai-api-key", "cookie", "set-cookie",
    "secret", "secret_key", "secret-key", "token", "access_token",
    "refresh_token", "auth-token", "channel_secret", "channel-key",
    "channel_key", "provider-secret", "private_key", "password", "passwd",
    "credential", "full_url", "url_query", "query_string", "upload",
    "uploaded_file", "prompt", "full_prompt", "response", "full_response",
    "tool_input", "tool_output", "tool_io",
)

# Explicitly allowed attribute keys (default-deny: anything not listed here is
# dropped). Covers the fixed gateway contract vocabulary + safe association.
_ALLOWED_EXACT_KEYS = frozenset({
    # generic gateway fields
    "name", "version", "request_id", "protocol", "route", "trace_origin",
    "upstream_trace_present", "span_role", "model",
    # router fields
    "requested_model", "resolved_model", "provider", "channel_id", "channel_type",
    "route_reason", "policy_name", "retry_count", "fallback_count", "attempt_count",
    "cache_status", "queue_duration_ms", "auth_duration_ms", "route_duration_ms",
    "total_duration_ms", "ttft_ms", "final_http_status_code", "final_error_type",
    "final_error_category",
    # attempt fields
    "attempt_index", "upstream_request_id", "upstream_http_status_code",
    "upstream_duration_ms", "upstream_connect_duration_ms", "upstream_ttft_ms",
    "timeout_ms", "retryable", "error_type", "error_category", "error_message",
    "finish_reason",
    # event attributes
    "reason", "delay_ms", "http_status_code",
    "from_channel_id", "to_channel_id",
    # usage / cost leaf names
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "reasoning_tokens", "cache_creation_tokens", "cache_read_tokens", "source",
    "input", "output", "total", "currency",
    # safe association / context
    "user_id", "session_id", "message_id", "app_name", "business_scenario",
    "status", "upstream_base_url_hash", "root_router_sampled",
})

# Namespaced keys allowed when their leaf is in the exact set (gateway.*,
# usage.*, cost.*, gen_ai.*, llm.*).
_ALLOWED_KEY_PREFIXES = ("gateway.", "usage.", "cost.", "gen_ai.", "llm.")


class PrivacyGuard:
    """Sanitize attributes, events, strings, and channel IDs (fail-closed).

    Usage:
        guard = PrivacyGuard(secret="<hmac-key>")
        sanitized = guard.sanitize_attributes({...})
        hashed = guard.hash_channel_id("channel-12")
    """

    def __init__(self, secret: Optional[str] = None, mask: str = REDACTED):
        """Args:
            secret: HMAC key for channel-id hashing. A stable hash (SHA-256 of
                the raw id) is used when no secret is configured.
            mask: Masking value (default ``<redacted>``).
        """
        self._secret = secret
        self._mask = mask

    # ── attribute sanitization ──

    def is_allowed_attribute(self, key: str) -> bool:
        """Return True when an attribute key is safe to record (default-deny)."""
        k = str(key).lower()
        if not k:
            return False
        # Default-deny sensitive prefixes always win.
        for prefix in _DENIED_KEY_PREFIXES:
            if k.startswith(prefix):
                return False
        # Namespaced keys: the leaf after the prefix must be explicitly allowed.
        for prefix in _ALLOWED_KEY_PREFIXES:
            if k.startswith(prefix):
                leaf = k[len(prefix):]
                return leaf in _ALLOWED_EXACT_KEYS
        return k in _ALLOWED_EXACT_KEYS

    def sanitize_attributes(self, attributes: Optional[dict]) -> dict:
        """Filter+mask an attribute dict; unknown/denied keys are dropped.

        Fail-closed: if the whole sanitization raises, an empty dict is
        returned so the span still proceeds.
        """
        if not attributes:
            return {}
        try:
            result = {}
            for key, value in attributes.items():
                if not self.is_allowed_attribute(str(key)):
                    continue
                result[str(key)] = self.sanitize_value(value)
            return result
        except Exception as e:
            logger.error("PrivacyGuard attribute sanitization failed: %s", e)
            return {}

    def sanitize_value(self, value: Any) -> Any:
        """Sanitize a single value; strings are secret-pattern masked."""
        if isinstance(value, str):
            return self.sanitize_string(value)
        if isinstance(value, (dict, list, tuple)):
            # Avoid unbounded recursion; deep-dive only shallow structures.
            try:
                if isinstance(value, dict):
                    return {
                        k: self.sanitize_value(v)
                        for k, v in list(value.items())[:64]
                    }
                return [self.sanitize_value(v) for v in list(value)[:64]]
            except Exception:
                return self._mask
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value
        return self._mask

    def sanitize_string(self, text: Optional[str]) -> str:
        """Mask secret patterns inside a string (fail-closed → <redacted>)."""
        if not isinstance(text, str) or not text:
            return text if isinstance(text, str) else ""
        try:
            masked = text
            for pattern, replacement in _SECRET_PATTERNS:
                masked = pattern.sub(replacement, masked)
            return masked
        except Exception:
            return self._mask

    # ── association top-level fields ──

    def sanitize_association(self, text: Optional[str]) -> str:
        """Sanitize a Router association top-level field (user_id / session_id
        / message_id / app_name / business_scene).

        Applies secret-pattern masking, control-character stripping, and a
        256-byte length limit — the same hardening the guarded span-attribute
        path applies, since these fields are external strings written to the
        Span record top-level (not via ``set_gateway_attribute``). Fail-closed.
        """
        if not isinstance(text, str) or not text:
            return text if isinstance(text, str) else ""
        try:
            masked = text
            for pattern, replacement in _SECRET_PATTERNS:
                masked = pattern.sub(replacement, masked)
            # Strip control chars (keep tab/newline out of telemetry fields).
            masked = "".join(ch for ch in masked if ch == " " or (ord(ch) >= 0x20 and ord(ch) != 0x7F))
            return _truncate_bytes(masked, _ASSOCIATION_MAX_BYTES)
        except Exception:
            return self._mask

    # ── channel ID hashing ──

    def hash_channel_id(self, raw_id: Optional[str]) -> Optional[str]:
        """Hash/HMAC an internal channel ID before recording.

        Uses HMAC-SHA256 when a secret is configured, else a plain SHA-256
        digest. Both are non-reversible from telemetry. Fail-closed: None on
        failure (the ID is simply not recorded).
        """
        if raw_id is None:
            return None
        try:
            raw = str(raw_id)
            if self._secret:
                return hmac.new(
                    self._secret.encode("utf-8"),
                    raw.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()[:16]
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        except Exception as e:
            logger.error("PrivacyGuard channel hash failed: %s", e)
            return None

    # ── URL sanitization ──

    def sanitize_url(self, url: Optional[str]) -> str:
        """Strip query-string secrets from a URL (keep scheme://host/path)."""
        if not url:
            return ""
        try:
            from urllib.parse import urlsplit, urlunsplit
            parts = urlsplit(str(url))
            # Strip full query by default (privacy spec).
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except Exception:
            return self._mask


# ── Unified guarded attribute entry point (P1-5) ──

# Per-key byte limits for external string values.
_MAX_STRING_BYTES = 512
# Byte limit for Router association top-level fields (user_id / session_id /
# message_id / app_name / business_scene) — written to the Span record, not via
# set_gateway_attribute, so the bound is applied in sanitize_association.
_ASSOCIATION_MAX_BYTES = 256
_PER_KEY_LIMITS = {
    "gateway.request_id": 256,
    "gateway.upstream_request_id": 256,
    "gateway.route": 256,
    "gateway.route_reason": 256,
    "gateway.provider": 128,
    "gateway.resolved_model": 128,
    "gateway.requested_model": 128,
    "gateway.error_message": _MAX_STRING_BYTES,
}


def _truncate_bytes(text: str, limit: int) -> str:
    """Truncate a string to at most ``limit`` UTF-8 bytes."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _normalize_value(value: Any) -> Any:
    """Type-normalize a value into a span-safe primitive."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return str(value)
    except Exception:
        return None


def set_gateway_attribute(span, key: str, value: Any, privacy_guard: Optional[PrivacyGuard]) -> bool:
    """Write one span attribute through the unified privacy guard.

    Pipeline: field-name whitelist (default-deny unknown keys) → value
    sanitization (secret masking, URL query stripping for ``gateway.route``)
    → per-key length limits → type normalization → ``span.set_attribute``.
    Fail-open: any failure logs and returns False without touching the span.

    Router and Attempt code MUST route external strings through this entry
    point instead of calling ``span.set_attribute`` directly.
    """
    if span is None or not key:
        return False
    try:
        guard = privacy_guard if privacy_guard is not None else PrivacyGuard()
        if not guard.is_allowed_attribute(str(key)):
            return False
        normalized = _normalize_value(value)
        if normalized is None:
            return False
        if isinstance(normalized, str):
            if str(key) == "gateway.route":
                normalized = guard.sanitize_url(normalized)
            else:
                normalized = guard.sanitize_string(normalized)
            limit = _PER_KEY_LIMITS.get(str(key), _MAX_STRING_BYTES)
            normalized = _truncate_bytes(normalized, limit)
        span.set_attribute(key, normalized)
        return True
    except Exception as e:
        logger.error("set_gateway_attribute failed (%s): %s", key, e)
        return False
