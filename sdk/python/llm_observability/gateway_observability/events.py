"""Gateway event names and the allowed event-attribute whitelist (spec §10).

Events use fixed ``gateway.*`` names. Event attributes are restricted to the
whitelist below; events never store the original payload by default.
"""
from typing import Final, FrozenSet

# ── Fixed gateway event names ──
EVENT_AUTH_STARTED: Final[str] = "gateway.auth.started"
EVENT_AUTH_COMPLETED: Final[str] = "gateway.auth.completed"
EVENT_AUTH_FAILED: Final[str] = "gateway.auth.failed"

EVENT_ROUTE_STARTED: Final[str] = "gateway.route.started"
EVENT_ROUTE_SELECTED: Final[str] = "gateway.route.selected"
EVENT_ROUTE_FAILED: Final[str] = "gateway.route.failed"
EVENT_MODEL_REMAPPED: Final[str] = "gateway.model.remapped"

EVENT_CACHE_HIT: Final[str] = "gateway.cache.hit"
EVENT_CACHE_MISS: Final[str] = "gateway.cache.miss"
EVENT_CACHE_BYPASS: Final[str] = "gateway.cache.bypass"

EVENT_RATE_LIMIT_CHECKED: Final[str] = "gateway.rate_limit.checked"
EVENT_RATE_LIMIT_REJECTED: Final[str] = "gateway.rate_limit.rejected"
EVENT_QUEUE_ENTERED: Final[str] = "gateway.queue.entered"
EVENT_QUEUE_EXITED: Final[str] = "gateway.queue.exited"

EVENT_ATTEMPT_STARTED: Final[str] = "gateway.attempt.started"
EVENT_ATTEMPT_FAILED: Final[str] = "gateway.attempt.failed"
EVENT_ATTEMPT_COMPLETED: Final[str] = "gateway.attempt.completed"

EVENT_RETRY_SCHEDULED: Final[str] = "gateway.retry.scheduled"
EVENT_FALLBACK_SELECTED: Final[str] = "gateway.fallback.selected"

EVENT_STREAM_STARTED: Final[str] = "gateway.stream.started"
EVENT_STREAM_FIRST_TOKEN: Final[str] = "gateway.stream.first_token"
EVENT_STREAM_COMPLETED: Final[str] = "gateway.stream.completed"
EVENT_STREAM_CANCELLED: Final[str] = "gateway.stream.cancelled"

EVENT_RESPONSE_COMPLETED: Final[str] = "gateway.response.completed"
EVENT_RESPONSE_FAILED: Final[str] = "gateway.response.failed"

EVENT_GATEWAY: Final[frozenset[str]] = frozenset({
    EVENT_AUTH_STARTED, EVENT_AUTH_COMPLETED, EVENT_AUTH_FAILED,
    EVENT_ROUTE_STARTED, EVENT_ROUTE_SELECTED, EVENT_ROUTE_FAILED,
    EVENT_MODEL_REMAPPED,
    EVENT_CACHE_HIT, EVENT_CACHE_MISS, EVENT_CACHE_BYPASS,
    EVENT_RATE_LIMIT_CHECKED, EVENT_RATE_LIMIT_REJECTED,
    EVENT_QUEUE_ENTERED, EVENT_QUEUE_EXITED,
    EVENT_ATTEMPT_STARTED, EVENT_ATTEMPT_FAILED, EVENT_ATTEMPT_COMPLETED,
    EVENT_RETRY_SCHEDULED, EVENT_FALLBACK_SELECTED,
    EVENT_STREAM_STARTED, EVENT_STREAM_FIRST_TOKEN, EVENT_STREAM_COMPLETED,
    EVENT_STREAM_CANCELLED,
    EVENT_RESPONSE_COMPLETED, EVENT_RESPONSE_FAILED,
})

# ── Allowed event attribute keys (spec §10) ──
ALLOWED_EVENT_ATTRIBUTES: Final[FrozenSet[str]] = frozenset({
    "reason",
    "attempt_index",
    "channel_id",
    "provider",
    "resolved_model",
    "delay_ms",
    "error_category",
    "http_status_code",
})


def is_allowed_event_attribute(key: str) -> bool:
    """Return True when an event attribute key is in the whitelist."""
    return key in ALLOWED_EVENT_ATTRIBUTES


def filter_event_attributes(attributes: dict) -> dict:
    """Filter an attribute dict down to the whitelist (fail-closed).

    Unknown keys are dropped, never stored.
    """
    return {
        key: value
        for key, value in (attributes or {}).items()
        if is_allowed_event_attribute(str(key))
    }
