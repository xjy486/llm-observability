"""Common privacy package — shared between SDK and Proxy.

This is the single source of truth for sensitive key sets and regex patterns.
Both sdk/python/llm_observability/utils/masking.py and proxy/config.py
import from this module.
"""
from .constants import SENSITIVE_KEYS, SENSITIVE_REGEX_PATTERNS

__all__ = ["SENSITIVE_KEYS", "SENSITIVE_REGEX_PATTERNS"]
