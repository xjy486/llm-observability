"""Payload masking utilities.

Strategies: off / metadata_only / masked / full
"""
import copy
from typing import Any

# Keys that are always masked regardless of strategy
SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "token", "secret",
    "password", "passwd", "credential",
}


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
    """Recursively mask sensitive keys in data."""
    if isinstance(data, dict):
        for key in list(data.keys()):
            kl = key.lower()
            if kl in SENSITIVE_KEYS:
                data[key] = "***REDACTED***"
            else:
                data[key] = _mask_recursive(data[key])
    elif isinstance(data, list):
        return [_mask_recursive(item) for item in data]
    return data


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
