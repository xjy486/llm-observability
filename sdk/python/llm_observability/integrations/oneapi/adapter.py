"""One-API GatewayAdapter implementation (spec §19).

Implements the GatewayAdapter ABC for One-API by delegating to the mappers.
The adapter ONLY extracts/maps/classifies — it never mutates One-API channel
selection, retry counts, timeouts, quota, or routing, and never swallows
business exceptions (spec §19.3).

Minimal internal-state contract (see the module docstring in
``gateway_observability/adapter.py`` for the generic contract):
  ``extract_route_decision(internal_state)`` accepts a mapping/object with:
      provider, channel_id | selected_channel_id, channel_type,
      requested_model, resolved_model | model, route_reason, policy_name,
      cache_status, rate_limited, fallback_channel_id / from_channel_id.
  ``extract_attempt_context(internal_state)`` accepts a mapping/object with:
      attempt_index, provider, channel_id, channel_type, resolved_model,
      upstream_base_url_hash, timeout_ms.
"""
from typing import Any, Optional

from ...gateway_observability.adapter import GatewayAdapter, _get
from ...gateway_observability.context import GatewayRequestContext
from ...gateway_observability.errors import GatewayError, classify_error
from ...gateway_observability.router_span import RouteDecision, AttemptContext
from .request_mapper import map_request
from .channel_mapper import map_channel, map_model_mapping, map_relay_mode
from .retry_mapper import map_route_state
from .usage_mapper import map_quota_to_usage


class OneApiAdapter(GatewayAdapter):
    """Glue-layer adapter for One-API (spec §19)."""

    def __init__(self, gateway_name: str = "one-api", relay_mode: Any = None):
        """Args:
            gateway_name: Gateway name recorded on the Router span.
            relay_mode: One-API relay mode override (default openai-compatible).
        """
        self._gateway_name = gateway_name
        self._relay_mode = relay_mode

    def extract_request_context(self, request: Any) -> GatewayRequestContext:
        rc = map_request(request)
        return GatewayRequestContext(
            gateway_name=self._gateway_name or rc.gateway_name,
            gateway_version=rc.gateway_version,
            request_id=rc.request_id,
            protocol=map_relay_mode(self._relay_mode) if self._relay_mode is not None else rc.protocol,
            route=rc.route,
            requested_model=rc.requested_model,
            user_id=rc.user_id,
            session_id=rc.session_id,
            message_id=rc.message_id,
            app_name=rc.app_name,
            business_scenario=rc.business_scenario,
        )

    def extract_route_decision(self, internal_state: Any) -> Optional[RouteDecision]:
        if internal_state is None:
            return None
        mapped = map_route_state(internal_state)
        channel = map_channel(_get(internal_state, "channel"))
        if mapped.get("channel_id") is None:
            mapped["channel_id"] = channel.get("channel_id")
        if mapped.get("channel_type") is None:
            mapped["channel_type"] = channel.get("channel_type")
        if mapped.get("provider") is None:
            mapped["provider"] = channel.get("provider")
        model_map = _get(internal_state, "model_mapping")
        model = map_model_mapping(model_map, mapped.get("requested_model"))
        if mapped.get("resolved_model") is None:
            mapped["resolved_model"] = model.get("resolved_model")
        return RouteDecision(**mapped)

    def extract_attempt_context(self, internal_state: Any) -> Optional[AttemptContext]:
        if internal_state is None:
            return None
        return AttemptContext(
            attempt_index=int(_get(internal_state, "attempt_index", 1) or 1),
            provider=_get(internal_state, "provider"),
            channel_id=_get(internal_state, "channel_id") or _get(internal_state, "selected_channel_id"),
            channel_type=_get(internal_state, "channel_type"),
            resolved_model=_get(internal_state, "resolved_model") or _get(internal_state, "model"),
            upstream_base_url_hash=_get(internal_state, "upstream_base_url_hash"),
            timeout_ms=_get(internal_state, "timeout_ms"),
        )

    def extract_usage(self, response: Any) -> Any:
        if response is None:
            return None
        if isinstance(response, dict):
            usage = response.get("usage")
            if usage is None:
                usage = map_quota_to_usage(response)
            return usage
        return getattr(response, "usage", None)

    def classify_error(self, error: BaseException) -> GatewayError:
        """Classify using the shared taxonomy. Business exceptions propagate."""
        return classify_error(error)
