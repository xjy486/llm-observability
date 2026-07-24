"""Payload masking utilities.

Strategies: off / metadata_only / masked / full

P1-3: Unified masking rules with Proxy — covers key-based + regex patterns:
  - api_key / authorization / token / secret / password (key-based)
  - sk-* (OpenAI-style keys)
  - Bearer tokens
  - Cookies
  - Text patterns: password=..., token=..., secret=...
"""
import copy
import re
from typing import Any

# Keys that are always masked regardless of strategy
SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "token", "secret",
    "password", "passwd", "credential", "cookie", "set-cookie",
}

# P1-3: Regex patterns for masking sensitive values in text content
SENSITIVE_PATTERNS = [
    # OpenAI-style API keys: sk-...
    (re.compile(r"sk-[a-zA-Z0-9]{20,}", re.IGNORECASE), "sk-***REDACTED***"),
    # Bearer tokens: Bearer xxx
    (re.compile(r"(?i)bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.IGNORECASE), "Bearer ***REDACTED***"),
    # password=xxx or password: xxx
    (re.compile(r"(?i)(password|passwd)\s*[=:]\s*\S+", re.IGNORECASE), r"\1=***REDACTED***"),
    # token=xxx or token: xxx
    (re.compile(r"(?i)(token|secret)\s*[=:]\s*\S+", re.IGNORECASE), r"\1=***REDACTED***"),
    # api_key=xxx
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+", re.IGNORECASE), "api_key=***REDACTED***"),
    # Authorization header value
    (re.compile(r"(?i)authorization\s*[=:]\s*\S+", re.IGNORECASE), "authorization=***REDACTED***"),
]


def mask_payload(data: Any, strategy: str = "masked") -> Any:
    """Mask payload data according to the given strategy.

    Args:
        data: The payload to mask (dict, list, or scalar).
        strategy: One of 'off', 'metadata_only', 'masked', 'full'.

    Returns:
        Masked copy of the data. Original is never modified.
    """
    if strategy == "full":
        return copy.deepcopy(data)
    if strategy == "off":
        return None
    if strategy == "metadata_only":
        return _extract_metadata(data)
    if strategy == "masked":
        return _mask_recursive(copy.deepcopy(data))
    return copy.deepcopy(data)


def _mask_recursive(data: Any) -> Any:
    """Recursively mask sensitive keys and patterns in data."""
    if isinstance(data, dict):
        for key in list(data.keys()):
            kl = key.lower()
            if kl in SENSITIVE_KEYS:
                data[key] = "***REDACTED***"
            else:
                data[key] = _mask_recursive(data[key])
    elif isinstance(data, list):
        return [_mask_recursive(item) for item in data]
    elif isinstance(data, str):
        return _mask_string_patterns(data)
    return data


def _mask_string_patterns(text: str) -> str:
    """Apply regex masking patterns to string content.

    P1-3: Masks sensitive patterns found in text content,
    such as 'my key is sk-xxxx' or 'password=secret123'.
    """
    if not isinstance(text, str):
        return text

    masked = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        masked = pattern.sub(
            replacement if not replacement.startswith(r"\1") else replacement,
            masked,
        )
    return masked


def _extract_metadata(data: Any) -> Any:
    """Extract only metadata (keys, counts), no content values."""
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                result[key] = _extract_metadata(value)
            elif isinstance(value, list):
                result[key] = {"count": len(value)}
            else:
                kl = key.lower()
                if kl in ("model", "stream", "n", "temperature", "max_tokens"):
                    result[key] = value
                else:
                    result[key] = "<redacted>"
        return result
    return "<redacted>"
