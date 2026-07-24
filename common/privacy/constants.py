"""Unified privacy constants shared between SDK and Proxy.

This module is the single source of truth for:
  - SENSITIVE_KEYS: keys whose values are entirely redacted in dicts
  - SENSITIVE_REGEX_PATTERNS: compiled regex patterns for text content masking

Both SDK (sdk/python/llm_observability/utils/masking.py) and
Proxy (proxy/config.py, proxy/payload.py) import from here.

BLOCKER-2: Previously, proxy/config.py used sys.path.insert to import
from the SDK source tree, which broke Docker builds (build context
only included ./proxy). This common module resolves that by providing
a proper shared package at the repo root.
"""
import re

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
# Both SDK and Proxy must use exactly this set.
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
