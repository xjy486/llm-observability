"""Gateway attribute-name constants (spec §9).

Frozen ``gateway.*`` / ``usage.*`` / ``cost.*`` attribute namespaces and the
``gateway.span_role`` role values. All GATEWAY spans carry exactly these keys —
never ad-hoc names — so Core/UI can query a fixed attribute vocabulary.
"""
from typing import Final

# ── gateway.span_role ──
ATTR_SPAN_ROLE: Final[str] = "gateway.span_role"
ROUTER: Final[str] = "router"
PROVIDER_ATTEMPT: Final[str] = "provider_attempt"

# ── Generic attributes (all GATEWAY spans) ──
ATTR_GATEWAY: Final[dict[str, str]] = {
    "name": "gateway.name",
    "version": "gateway.version",
    "request_id": "gateway.request_id",
    "protocol": "gateway.protocol",
    "route": "gateway.route",
    "trace_origin": "gateway.trace_origin",
    "upstream_trace_present": "gateway.upstream_trace_present",
    "span_role": "gateway.span_role",
}

# ── Router span attributes ──
ATTR_ROUTER: Final[dict[str, str]] = {
    "requested_model": "gateway.requested_model",
    "resolved_model": "gateway.resolved_model",
    "provider": "gateway.provider",
    "channel_id": "gateway.channel_id",
    "channel_type": "gateway.channel_type",
    "route_reason": "gateway.route_reason",
    "policy_name": "gateway.policy_name",
    "retry_count": "gateway.retry_count",
    "fallback_count": "gateway.fallback_count",
    "attempt_count": "gateway.attempt_count",
    "cache_status": "gateway.cache_status",
    "queue_duration_ms": "gateway.queue_duration_ms",
    "auth_duration_ms": "gateway.auth_duration_ms",
    "route_duration_ms": "gateway.route_duration_ms",
    "total_duration_ms": "gateway.total_duration_ms",
    "ttft_ms": "gateway.ttft_ms",
    "final_http_status_code": "gateway.final_http_status_code",
    "final_error_type": "gateway.final_error_type",
    "final_error_category": "gateway.final_error_category",
}

# ── Attempt span attributes ──
ATTR_ATTEMPT: Final[dict[str, str]] = {
    "attempt_index": "gateway.attempt_index",
    "provider": "gateway.provider",
    "channel_id": "gateway.channel_id",
    "channel_type": "gateway.channel_type",
    "resolved_model": "gateway.resolved_model",
    "upstream_request_id": "gateway.upstream_request_id",
    "upstream_http_status_code": "gateway.upstream_http_status_code",
    "upstream_duration_ms": "gateway.upstream_duration_ms",
    "upstream_connect_duration_ms": "gateway.upstream_connect_duration_ms",
    "upstream_ttft_ms": "gateway.upstream_ttft_ms",
    "timeout_ms": "gateway.timeout_ms",
    "retryable": "gateway.retryable",
    "error_type": "gateway.error_type",
    "error_category": "gateway.error_category",
    "error_message": "gateway.error_message",
    "finish_reason": "gateway.finish_reason",
}

# ── Usage attributes ──
ATTR_USAGE: Final[dict[str, str]] = {
    "input_tokens": "usage.input_tokens",
    "output_tokens": "usage.output_tokens",
    "total_tokens": "usage.total_tokens",
    "cached_input_tokens": "usage.cached_input_tokens",
    "reasoning_tokens": "usage.reasoning_tokens",
    "cache_creation_tokens": "usage.cache_creation_tokens",
    "cache_read_tokens": "usage.cache_read_tokens",
    "source": "usage.source",
}

# ── Cost attributes ──
ATTR_COST: Final[dict[str, str]] = {
    "input": "cost.input",
    "output": "cost.output",
    "total": "cost.total",
    "currency": "cost.currency",
    "source": "cost.source",
}


def get_gateway_attr(name: str) -> str:
    """Return the full attribute key for a generic gateway field."""
    return ATTR_GATEWAY[name]


def get_usage_attr(name: str) -> str:
    """Return the full attribute key for a usage field."""
    return ATTR_USAGE[name]


def get_cost_attr(name: str) -> str:
    """Return the full attribute key for a cost field."""
    return ATTR_COST[name]
