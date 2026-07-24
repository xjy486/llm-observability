"""Payload masking utilities.

Strategies: off / metadata_only / masked / full

P1-4 / BLOCKER-2: Unified masking rules with Proxy — imports canonical
SENSITIVE_KEYS and SENSITIVE_REGEX_PATTERNS from common.privacy.constants
to ensure both SDK and Proxy share the exact same sensitive key set and
regex patterns.
"""
import copy
import re
import sys
import os
from typing import Any

# BLOCKER-2: Import from common/privacy module (repo-root shared package)
# Path: masking.py → utils/ → llm_observability/ → python/ → sdk/ → repo_root
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Fallback: also try adding the llm-observability root
_LLM_OBS_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _LLM_OBS_ROOT not in sys.path:
    sys.path.insert(0, _LLM_OBS_ROOT)

try:
    from common.privacy.constants import (
        SENSITIVE_KEYS as _CANONICAL_SENSITIVE_KEYS,
        SENSITIVE_REGEX_PATTERNS as _CANONICAL_REGEX_PATTERNS,
    )
except ImportError:
    # Fallback for SDK installed as package (without repo root available)
    # In this case, use inline constants to ensure masking still works
    import re as _re
    _CANONICAL_SENSITIVE_KEYS = [
        "authorization", "api_key", "apikey", "api-key", "x-api-key",
        "x-auth-token", "token", "access_token", "refresh_token",
        "secret", "secret_key", "private_key", "password", "passwd",
        "credential", "cookie", "set-cookie", "proxy-authorization",
    ]
    _CANONICAL_REGEX_PATTERNS = [
        (_re.compile(r"sk-[a-zA-Z0-9]{20,}", _re.IGNORECASE), "sk-***REDACTED***"),
        (_re.compile(r"(?i)bearer\s+[a-zA-Z0-9\-._~+/]+=*", _re.IGNORECASE), "Bearer ***REDACTED***"),
        (_re.compile(r"(?i)(password|passwd)\s*[=:]\s*\S+", _re.IGNORECASE), r"\1=***REDACTED***"),
        (_re.compile(r"(?i)(token)\s*[=:]\s*\S+", _re.IGNORECASE), r"\1=***REDACTED***"),
        (_re.compile(r"(?i)(secret)\s*[=:]\s*\S+", _re.IGNORECASE), r"\1=***REDACTED***"),
        (_re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+", _re.IGNORECASE), "api_key=***REDACTED***"),
        (_re.compile(r"(?i)authorization\s*[=:]\s*\S+", _re.IGNORECASE), "authorization=***REDACTED***"),
    ]

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
