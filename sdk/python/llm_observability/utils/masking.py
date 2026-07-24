"""Payload masking utilities.

Strategies: off / metadata_only / masked / full

P1-4: Unified masking rules with Proxy — imports canonical SENSITIVE_KEYS
and SENSITIVE_REGEX_PATTERNS from privacy_constants to ensure both SDK and
Proxy share the exact same sensitive key set and regex patterns.
"""
import copy
import re
from typing import Any

# P1-4: Import unified constants from privacy_constants (single source of truth)
from llm_observability.utils.privacy_constants import (
    SENSITIVE_KEYS as _CANONICAL_SENSITIVE_KEYS,
    SENSITIVE_REGEX_PATTERNS as _CANONICAL_REGEX_PATTERNS,
)

# Keys that are always masked regardless of strategy (from unified constants)
SENSITIVE_KEYS = set(_CANONICAL_SENSITIVE_KEYS)

# P1-4: Regex patterns from unified constants
SENSITIVE_PATTERNS = _CANONICAL_REGEX_PATTERNS


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

    P1-4: Uses unified SENSITIVE_REGEX_PATTERNS from privacy_constants.
    Masks sensitive patterns found in text content, such as
    'my key is sk-xxxx' or 'password=secret123'.
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
