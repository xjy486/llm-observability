"""Fixed gateway error taxonomy and safe error messages (spec §11).

Error messages are safe strings: length-limited, sanitized, and fail-closed.
Authorization headers, API keys, cookies, provider secrets, full URL queries,
full response bodies, and full stack traces never enter telemetry.
"""
import logging
from dataclasses import dataclass
from typing import Final, Optional

from .privacy import PrivacyGuard

logger = logging.getLogger("llm_obs.gateway.errors")


class ErrorCategory:
    """Fixed error taxonomy (spec §11)."""
    AUTHENTICATION: Final[str] = "authentication"
    AUTHORIZATION: Final[str] = "authorization"
    RATE_LIMIT: Final[str] = "rate_limit"
    QUOTA: Final[str] = "quota"
    TIMEOUT: Final[str] = "timeout"
    CONNECT_ERROR: Final[str] = "connect_error"
    DNS_ERROR: Final[str] = "dns_error"
    TLS_ERROR: Final[str] = "tls_error"
    PROVIDER_4XX: Final[str] = "provider_4xx"
    PROVIDER_5XX: Final[str] = "provider_5xx"
    INVALID_REQUEST: Final[str] = "invalid_request"
    INVALID_RESPONSE: Final[str] = "invalid_response"
    STREAM_INTERRUPTED: Final[str] = "stream_interrupted"
    CLIENT_CANCELLED: Final[str] = "client_cancelled"
    GATEWAY_INTERNAL: Final[str] = "gateway_internal"
    UNKNOWN: Final[str] = "unknown"

    ALL: Final[frozenset[str]] = frozenset({
        AUTHENTICATION, AUTHORIZATION, RATE_LIMIT, QUOTA, TIMEOUT,
        CONNECT_ERROR, DNS_ERROR, TLS_ERROR, PROVIDER_4XX, PROVIDER_5XX,
        INVALID_REQUEST, INVALID_RESPONSE, STREAM_INTERRUPTED,
        CLIENT_CANCELLED, GATEWAY_INTERNAL, UNKNOWN,
    })


# Categories that are safe to retry (spec §11 / §13).
_RETRYABLE_CATEGORIES: frozenset[str] = frozenset({
    ErrorCategory.TIMEOUT,
    ErrorCategory.CONNECT_ERROR,
    ErrorCategory.DNS_ERROR,
    ErrorCategory.TLS_ERROR,
    ErrorCategory.PROVIDER_5XX,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.QUOTA,
    ErrorCategory.STREAM_INTERRUPTED,
    ErrorCategory.GATEWAY_INTERNAL,
})


def is_retryable_category(category: Optional[str]) -> bool:
    """Return True when a category is considered retryable."""
    return category in _RETRYABLE_CATEGORIES


# Max length for a recorded error message (safe-string bound).
MAX_ERROR_MESSAGE_LENGTH: Final[int] = 512


@dataclass
class GatewayError:
    """Classified gateway/upstream failure (spec §11).

    Attributes:
        category: Fixed ErrorCategory value.
        type: Optional concrete exception type name.
        message: Safe, sanitized, length-limited message.
        retryable: Whether retrying this failure is safe.
    """
    category: str = ErrorCategory.UNKNOWN
    type: Optional[str] = None
    message: Optional[str] = None
    retryable: bool = False


def safe_error_message(exc: BaseException) -> str:
    """Extract a safe error message without raising.

    str(exc) can itself raise; this never propagates. The message is
    sanitized (secrets stripped) and length-limited (fail-closed).
    """
    try:
        text = str(exc)
    except Exception:
        text = "<error message unavailable>"
    if not text:
        text = type(exc).__name__
    # Sanitize secrets via the same masking rules as telemetry attributes.
    try:
        text = PrivacyGuard().sanitize_string(text)
    except Exception:
        text = "<redacted>"
    if len(text) > MAX_ERROR_MESSAGE_LENGTH:
        text = text[:MAX_ERROR_MESSAGE_LENGTH]
    return text


def classify_error(exc: BaseException) -> GatewayError:
    """Classify a gateway/upstream exception into the fixed taxonomy.

    Uses well-known exception types; falls back to ``unknown`` with
    ``retryable=False`` (fail-closed on the safe side).
    """
    category = ErrorCategory.UNKNOWN
    retryable = False
    name = type(exc).__name__

    if isinstance(exc, (TimeoutError,)):
        category = ErrorCategory.TIMEOUT
        retryable = True
    elif isinstance(exc, ConnectionError):
        category = ErrorCategory.CONNECT_ERROR
        retryable = True
    elif isinstance(exc, OSError):
        import socket
        if isinstance(exc, socket.timeout):
            category = ErrorCategory.TIMEOUT
            retryable = True
        elif exc.errno is not None and exc.errno in (socket.EHOSTUNREACH, socket.ENETUNREACH, socket.ENETDOWN):
            category = ErrorCategory.CONNECT_ERROR
            retryable = True
        else:
            category = ErrorCategory.UNKNOWN
            retryable = False

    # Provider HTTP status classification (used when the caller has a status).
    # Keep error message sanitized + length-limited.
    message = safe_error_message(exc)
    return GatewayError(category=category, type=name, message=message, retryable=retryable)


def classify_http_status(status_code: Optional[int]) -> str:
    """Classify an HTTP status code into the error taxonomy.

    Returns the category string; non-error statuses map to ``unknown``.
    """
    if status_code is None:
        return ErrorCategory.UNKNOWN
    if 400 <= status_code < 500:
        if status_code == 401:
            return ErrorCategory.AUTHENTICATION
        if status_code == 403:
            return ErrorCategory.AUTHORIZATION
        if status_code == 429:
            return ErrorCategory.RATE_LIMIT
        return ErrorCategory.PROVIDER_4XX
    if status_code >= 500:
        return ErrorCategory.PROVIDER_5XX
    return ErrorCategory.UNKNOWN
