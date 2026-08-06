"""Gateway Native Observability — Router/Attempt GATEWAY contract + runtime.

Public API:
    RouterSpan        — context manager for a Router GATEWAY span (role="router")
    AttemptSpan       — context manager for an Attempt GATEWAY span (role="provider_attempt")
    GatewayRuntime    — orchestrator: adapter → RouterSpan → AttemptSpan → recorder → reporter
    GatewayEventRecorder — fixed gateway.* event recorder (whitelisted attributes)
    GatewayRequestContext / RouteDecision / AttemptContext / NormalizedUsage / NormalizedCost
    GatewayAdapter    — ABC implemented by GenericAdapter / OneApiAdapter / LiteLLMAdapter
    PrivacyGuard / UsageNormalizer / CostCalculator

Design (spec §4-§18):
    - No new SpanKind; Router and Attempt are both SpanKind.GATEWAY distinguished
      via ``gateway.span_role`` (``router`` | ``provider_attempt``).
    - Router parent = SDK LLM span when present, else Root.
    - Attempt parent = Router span; every real upstream request gets one unique Attempt.
    - Fail-open everywhere: telemetry failures never change gateway business behavior.
"""
from .attributes import (
    ATTR_GATEWAY, ATTR_USAGE, ATTR_COST,
    ATTR_SPAN_ROLE, ROUTER, PROVIDER_ATTEMPT,
    get_gateway_attr, get_usage_attr, get_cost_attr,
)
from .events import (
    EVENT_GATEWAY, EVENT_RETRY_SCHEDULED, EVENT_FALLBACK_SELECTED,
    EVENT_STREAM_STARTED, EVENT_STREAM_FIRST_TOKEN, EVENT_STREAM_COMPLETED,
    EVENT_STREAM_CANCELLED, EVENT_RESPONSE_COMPLETED, EVENT_RESPONSE_FAILED,
    EVENT_ATTEMPT_STARTED, EVENT_ATTEMPT_COMPLETED, EVENT_ATTEMPT_FAILED,
    EVENT_ROUTE_SELECTED, EVENT_CACHE_HIT, EVENT_RATE_LIMIT_REJECTED,
    ALLOWED_EVENT_ATTRIBUTES,
)
from .errors import (
    ErrorCategory,
    GatewayError,
    classify_error,
    safe_error_message,
    is_retryable_category,
)
from .context import (
    GatewayRequestContext,
    GatewayContext,
    get_gateway_context,
    set_gateway_context,
    reset_gateway_context,
    reset_gateway_context_fail_open,
)
from .usage import NormalizedUsage, UsageNormalizer, add_usage, usage_to_attributes, usage_has_values
from .cost import NormalizedCost, CostCalculator, add_cost, cost_to_attributes
from .router_span import RouterSpan, RouteDecision, AttemptContext, AttemptResult
from .attempt_span import AttemptSpan
from .privacy import PrivacyGuard, set_gateway_attribute
from .propagation import (
    sampling_from_traceparent,
    ROOT_ROUTER_SAMPLED_ATTR,
    inject_downstream_trace_headers,
)
from .recorder import GatewayEventRecorder
from .runtime import GatewayRuntime
from .adapter import GatewayAdapter, GenericAdapter
from .streaming import GatewayStream, AsyncGatewayStream, wrap_stream, wrap_async_stream
from .aggregation import router_usage_for_llm, apply_router_usage_to_span

__all__ = [
    # attributes
    "ATTR_GATEWAY", "ATTR_USAGE", "ATTR_COST", "ATTR_SPAN_ROLE",
    "ROUTER", "PROVIDER_ATTEMPT",
    "get_gateway_attr", "get_usage_attr", "get_cost_attr",
    # events
    "EVENT_GATEWAY", "EVENT_RETRY_SCHEDULED", "EVENT_FALLBACK_SELECTED",
    "EVENT_STREAM_STARTED", "EVENT_STREAM_FIRST_TOKEN", "EVENT_STREAM_COMPLETED",
    "EVENT_STREAM_CANCELLED", "EVENT_RESPONSE_COMPLETED", "EVENT_RESPONSE_FAILED",
    "EVENT_ATTEMPT_STARTED", "EVENT_ATTEMPT_COMPLETED", "EVENT_ATTEMPT_FAILED",
    "EVENT_ROUTE_SELECTED", "EVENT_CACHE_HIT", "EVENT_RATE_LIMIT_REJECTED",
    "ALLOWED_EVENT_ATTRIBUTES",
    # errors
    "ErrorCategory", "GatewayError", "classify_error", "safe_error_message",
    "is_retryable_category",
    # data models
    "GatewayRequestContext", "RouteDecision", "AttemptContext",
    "NormalizedUsage", "NormalizedCost",
    # context
    "GatewayContext", "get_gateway_context", "set_gateway_context",
    "reset_gateway_context", "reset_gateway_context_fail_open",
    # runtime
    "RouterSpan", "AttemptSpan", "GatewayRuntime", "GatewayEventRecorder",
    # usage/cost
    "UsageNormalizer", "CostCalculator",
    "add_usage", "usage_to_attributes", "usage_has_values",
    "add_cost", "cost_to_attributes",
    # privacy / propagation
    "PrivacyGuard", "set_gateway_attribute",
    "sampling_from_traceparent", "ROOT_ROUTER_SAMPLED_ATTR",
    "inject_downstream_trace_headers",
    # adapters
    "GatewayAdapter", "GenericAdapter",
    # streaming + LLM aggregation hook
    "GatewayStream", "AsyncGatewayStream", "wrap_stream", "wrap_async_stream",
    "router_usage_for_llm", "apply_router_usage_to_span",
]
