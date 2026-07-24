"""Unified privacy constants shared between SDK and Proxy.

P1-4: Both SDK and Proxy must use the same canonical set of sensitive keys
and regex patterns for masking. This module is the single source of truth.

Import this from:
  - sdk/python/llm_observability/utils/masking.py
  - proxy/config.py
"""

# ── Canonical sensitive key set ──
# Keys whose VALUES are entirely redacted when found in dicts (case-insensitive).
# Both SDK and Proxy must use exactly this set.
SENSITIVE_KEYS = [
    "authorization",
    "api_key",
    "apikey",
    "api-key",
    "x-api-key",
    "x-auth-token",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "secret_key",
    "private_key",
    "password",
    "passwd",
    "credential",
    "cookie",
    "set-cookie",
    "proxy-authorization",
]

# ── Canonical regex patterns for text content masking ──
# Each entry is (compiled_regex, replacement_string).
# Used for masking sensitive values embedded in text content.
import re

SENSITIVE_REGEX_PATTERNS = [
    # OpenAI-style API keys: sk-...
    (re.compile(r"sk-[a-zA-Z0-9]{20,}", re.IGNORECASE), "sk-***REDACTED***"),
    # Bearer tokens: Bearer xxx
    (re.compile(r"(?i)bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.IGNORECASE), "Bearer ***REDACTED***"),
    # password=xxx or password: xxx or passwd=xxx
    (re.compile(r"(?i)(password|passwd)\s*[=:]\s*\S+", re.IGNORECASE), r"\1=***REDACTED***"),
    # token=xxx or token: xxx
    (re.compile(r"(?i)(token)\s*[=:]\s*\S+", re.IGNORECASE), r"\1=***REDACTED***"),
    # secret=xxx or secret: xxx
    (re.compile(r"(?i)(secret)\s*[=:]\s*\S+", re.IGNORECASE), r"\1=***REDACTED***"),
    # api_key=xxx or api-key=xxx
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+", re.IGNORECASE), "api_key=***REDACTED***"),
    # Authorization header value
    (re.compile(r"(?i)authorization\s*[=:]\s*\S+", re.IGNORECASE), "authorization=***REDACTED***"),
]
