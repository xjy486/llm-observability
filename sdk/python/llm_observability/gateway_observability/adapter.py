"""GatewayAdapter ABC and GenericAdapter reference implementation (spec §7).

An adapter extracts request/route/attempt/usage facts and classifies errors
from a gateway's internal state. Adapters NEVER persist, generate trace IDs,
report over HTTP, mutate routing/retry/timeout/quota, or swallow business
exceptions — those are the runtime's job (spec §7.2, oneapi spec §4).
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

from .context import GatewayRequestContext
from .errors import GatewayError, classify_error
from .router_span import RouteDecision, AttemptContext

logger = logging.getLogger("llm_obs.gateway.adapter")


class GatewayAdapter(ABC):
    """Abstract gateway glue-layer contract (spec §7.1).

    The ``internal_state`` passed to ``extract_route_decision`` /
    ``extract_attempt_context`` is intentionally loose (``Any``) — see the
    per-gateway minimal internal-state contract documented in the adapter
    packages (e.g. integrations/oneapi/docs). A generic best-effort contract
    is described below.

    Minimal generic ``internal_state`` contract:
      ``extract_route_decision(internal_state)`` accepts a mapping (or object)
      that may contain: ``provider``, ``channel_id``, ``channel_type``,
      ``requested_model``, ``resolved_model``, ``route_reason``,
      ``policy_name``, ``fallback_from_channel_id``, ``retryable``,
      ``cache_status``, ``rate_limited``.
      ``extract_attempt_context(internal_state)`` accepts a mapping (or
      object) that may contain: ``attempt_index``, ``provider``,
      ``channel_id``, ``channel_type``, ``resolved_model``,
      ``upstream_base_url_hash``, ``timeout_ms``.
    Unknown keys are ignored; missing keys yield None defaults.
    """

    @abstractmethod
    def extract_request_context(self, request: Any) -> GatewayRequestContext:
        """Extract immutable gateway request facts."""

    @abstractmethod
    def extract_route_decision(self, internal_state: Any) -> Optional[RouteDecision]:
        """Extract the routing decision made by the gateway."""

    @abstractmethod
    def extract_attempt_context(self, internal_state: Any) -> Optional[AttemptContext]:
        """Extract the context of one real upstream attempt."""

    @abstractmethod
    def extract_usage(self, response: Any) -> Any:
        """Extract the raw usage payload from an upstream response."""

    @abstractmethod
    def classify_error(self, error: BaseException) -> GatewayError:
        """Classify a gateway/upstream failure into the fixed taxonomy."""


def _get(state: Any, key: str, default=None):
    """Best-effort attribute/dict access (never raises)."""
    if state is None:
        return default
    try:
        if isinstance(state, dict):
            if key in state:
                return state[key]
        elif hasattr(state, key):
            return getattr(state, key)
        # case-insensitive dict fallback
        if isinstance(state, dict):
            lowered = {str(k).lower(): v for k, v in state.items()}
            return lowered.get(key.lower(), default)
    except Exception:
        pass
    return default


class GenericAdapter(GatewayAdapter):
    """Reference implementation operating on the generic internal-state contract.

    Useful for the mock gateway harness and as a template for gateway-specific
    adapters. It only extracts and maps — it never mutates gateway state.
    """

    def extract_request_context(self, request: Any) -> GatewayRequestContext:
        rc = GatewayRequestContext(
            gateway_name=_get(request, "gateway_name", "unknown") or "unknown",
            gateway_version=_get(request, "gateway_version"),
            request_id=_get(request, "request_id"),
            protocol=_get(request, "protocol", "openai-compatible") or "openai-compatible",
            route=_get(request, "route", ""),
            requested_model=_get(request, "requested_model") or _get(request, "model"),
            user_id=_get(request, "user_id"),
            session_id=_get(request, "session_id"),
            message_id=_get(request, "message_id"),
            app_name=_get(request, "app_name"),
            business_scenario=_get(request, "business_scenario"),
        )
        return rc

    def extract_route_decision(self, internal_state: Any) -> Optional[RouteDecision]:
        if internal_state is None:
            return None
        rd = RouteDecision(
            provider=_get(internal_state, "provider"),
            channel_id=_get(internal_state, "channel_id"),
            channel_type=_get(internal_state, "channel_type"),
            requested_model=_get(internal_state, "requested_model"),
            resolved_model=_get(internal_state, "resolved_model"),
            route_reason=_get(internal_state, "route_reason"),
            policy_name=_get(internal_state, "policy_name"),
            fallback_from_channel_id=_get(internal_state, "fallback_from_channel_id"),
            retryable=bool(_get(internal_state, "retryable", False)),
            cache_status=_get(internal_state, "cache_status"),
            rate_limited=bool(_get(internal_state, "rate_limited", False)),
        )
        return rd

    def extract_attempt_context(self, internal_state: Any) -> Optional[AttemptContext]:
        if internal_state is None:
            return None
        return AttemptContext(
            attempt_index=int(_get(internal_state, "attempt_index", 1) or 1),
            provider=_get(internal_state, "provider"),
            channel_id=_get(internal_state, "channel_id"),
            channel_type=_get(internal_state, "channel_type"),
            resolved_model=_get(internal_state, "resolved_model"),
            upstream_base_url_hash=_get(internal_state, "upstream_base_url_hash"),
            timeout_ms=_get(internal_state, "timeout_ms"),
        )

    def extract_usage(self, response: Any) -> Any:
        """Extract the raw usage payload from a response (dict or object)."""
        if response is None:
            return None
        try:
            if isinstance(response, dict):
                return response.get("usage")
            return getattr(response, "usage", None)
        except Exception:
            return None

    def classify_error(self, error: BaseException) -> GatewayError:
        """Classify using the shared taxonomy (timeout/connect/etc.)."""
        return classify_error(error)
