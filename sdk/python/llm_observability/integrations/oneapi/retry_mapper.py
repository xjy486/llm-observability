"""One-API retry / fallback mapping to gateway decisions (spec §19.2).

These mappers describe the *decision* so the runtime can emit events and fresh
Attempt spans — they never execute the retry or change retry counts.
"""
from typing import Any, Optional

from ...gateway_observability.adapter import _get


def map_retry_state(internal_state: Any) -> Optional[dict]:
    """Extract a retry decision from One-API internal state.

    Returns:
        {attempt_index, delay_ms, reason} when a retry was scheduled, else None.
    """
    retries = _get(internal_state, "retries")
    attempt_index = _get(internal_state, "attempt_index", 1) or 1
    delay_ms = _get(internal_state, "retry_delay_ms")
    reason = _get(internal_state, "retry_reason")
    if reason is None:
        reason = "retry"
    if retries is None and delay_ms is None and reason is None:
        return None
    return {
        "attempt_index": int(attempt_index) + 1,
        "delay_ms": delay_ms,
        "reason": reason,
    }


def map_fallback_state(internal_state: Any) -> Optional[dict]:
    """Extract a fallback decision from One-API internal state.

    Returns:
        {from_channel_id, to_channel_id, reason} when a fallback was selected.
    """
    to_channel = _get(internal_state, "fallback_channel_id") or _get(internal_state, "to_channel_id")
    from_channel = _get(internal_state, "from_channel_id")
    reason = _get(internal_state, "fallback_reason")
    if to_channel is None:
        return None
    return {
        "from_channel_id": from_channel,
        "to_channel_id": to_channel,
        "reason": reason or "fallback",
    }


def map_route_state(internal_state: Any) -> dict:
    """Extract the One-API routing decision (channel selection result)."""
    return {
        "provider": _get(internal_state, "provider"),
        "channel_id": _get(internal_state, "channel_id") or _get(internal_state, "selected_channel_id"),
        "channel_type": _get(internal_state, "channel_type"),
        "requested_model": _get(internal_state, "requested_model"),
        "resolved_model": _get(internal_state, "resolved_model") or _get(internal_state, "model"),
        "route_reason": _get(internal_state, "route_reason"),
        "policy_name": _get(internal_state, "policy_name"),
        "cache_status": _get(internal_state, "cache_status"),
        "rate_limited": bool(_get(internal_state, "rate_limited", False)),
    }
