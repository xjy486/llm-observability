"""Unified Association Propagation — Phase 2.5 final closeout (P0-4).

Single source of truth for W3C baggage encode/decode/build/parse/merge,
reused by SDK Distributed, OpenAI propagation, and Proxy (copied as a
shared contract).

Priority (frozen):
    local span explicit > local association context > Compat header
    > W3C baggage > None

Field set: user, session_id, message_id, business_scenario, app_name
Aliases: user_id -> user, business_scene -> business_scenario

Security: carrier never carries Prompt/Response/API Key/Tool Output.
Values are fail-closed sanitized (masking failure -> '<redacted>'),
length-limited, control-char handled.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote, unquote

logger = logging.getLogger("llm_obs.association_propagation")

# Canonical association fields
CANONICAL_FIELDS = ("user", "session_id", "message_id", "business_scenario", "app_name")

# Alias -> canonical
ALIASES = {
    "user_id": "user",
    "business_scene": "business_scenario",
}

MAX_VALUE_LENGTH = 256


def _sanitize_value(value: Any, max_length: int = MAX_VALUE_LENGTH) -> Optional[str]:
    """Fail-closed sanitization of an association value.

    Applies control-char handling and truncation. Masking failures return
    '<redacted>'. The actual pattern masking is delegated to the SDK's
    masking module when available; this function is the pure fallback used
    by the Proxy (which cannot import the SDK).
    """
    if value is None:
        return None
    try:
        text = str(value)
        # Strip ALL control characters (including CR/LF/tab) to prevent
        # log injection / header injection / UI display issues.
        text = "".join(ch for ch in text if ord(ch) >= 0x20 or ch == " ")
        return text[:max_length]
    except Exception:
        return "<redacted>"


def encode_baggage_value(value: str) -> str:
    """W3C baggage percent-encoding for a value.

    Encodes commas, equals, spaces, and non-token characters so the baggage
    header remains parseable. Control characters are also encoded.
    """
    if value is None:
        return ""
    return quote(str(value), safe="")


def decode_baggage_value(value: str) -> str:
    """Percent-decode a W3C baggage value."""
    if value is None:
        return ""
    try:
        return unquote(str(value))
    except Exception:
        return str(value)


def build_association_baggage(props: dict) -> str:
    """Build a W3C baggage header string from association properties dict.

    `props` keys are canonical field names. Returns a comma-separated
    key=value string with percent-encoded values, or empty string if no
    non-None values.
    """
    parts = []
    for field in CANONICAL_FIELDS:
        val = props.get(field)
        if val is not None:
            parts.append(f"{field}={encode_baggage_value(str(val))}")
    return ",".join(parts)


def parse_association_baggage(header: str) -> dict:
    """Parse a W3C baggage header into a dict of canonical field -> value.

    Only known association fields are extracted. Values are percent-decoded
    and sanitized.
    """
    result: dict[str, str] = {}
    if not header:
        return result
    try:
        for pair in str(header).split(","):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                k = k.strip()
                canonical = ALIASES.get(k, k)
                if canonical in CANONICAL_FIELDS:
                    decoded = decode_baggage_value(v.strip())
                    result[canonical] = _sanitize_value(decoded)
    except Exception:
        logger.debug("baggage parse failed", exc_info=True)
    return result


def merge_remote_association(baggage: Optional[str], compat_headers: dict) -> dict:
    """Merge remote association from baggage + compat headers.

    Priority (frozen): Compat header > W3C baggage.
    `compat_headers` is a dict of already-extracted compat values keyed by
    canonical field name (user, session_id, message_id, business_scenario,
    app_name).
    """
    merged = parse_association_baggage(baggage) if baggage else {}
    # Compat headers override baggage
    for field in CANONICAL_FIELDS:
        val = compat_headers.get(field)
        if val is not None:
            merged[field] = _sanitize_value(val)
    return merged


# Compat header name -> canonical field
COMPAT_HEADER_MAP = {
    "x-user-id": "user",
    "x-session-id": "session_id",
    "x-business-scene": "business_scenario",
    "x-app-name": "app_name",
}


def extract_compat_headers(headers: dict) -> dict:
    """Extract compat association headers (case-insensitive) from a headers dict.

    Returns canonical field -> value. Used together with parse_association_baggage.
    """
    result: dict[str, str] = {}
    if not headers:
        return result
    lower_map: dict[str, Any] = {}
    for k, v in (headers.items() if hasattr(headers, "items") else headers):
        lower_map[str(k).lower()] = v
    for header, field in COMPAT_HEADER_MAP.items():
        val = lower_map.get(header)
        if isinstance(val, (list, tuple)) and val:
            val = val[0]
        if val:
            result[field] = _sanitize_value(val)
    return result
