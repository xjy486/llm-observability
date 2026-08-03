"""LiteLLM GatewayAdapter — reserved interface stub (spec §7, oneapi spec).

Full LiteLLM support is Phase 3.1. This stub pins the replaceable adapter
surface so LiteLLM glue can be added without touching the gateway runtime.
"""
from typing import Any, Optional

from ...gateway_observability.adapter import GatewayAdapter
from ...gateway_observability.context import GatewayRequestContext
from ...gateway_observability.errors import GatewayError
from ...gateway_observability.router_span import RouteDecision, AttemptContext
from ...gateway_observability.usage import NormalizedUsage


class LiteLLMAdapter(GatewayAdapter):
    """Reserved adapter interface for future LiteLLM support.

    All methods raise NotImplementedError — wiring LiteLLM internals
    (router config, retry policy, model group mapping) is Phase 3.1 scope.
    """

    def extract_request_context(self, request: Any) -> GatewayRequestContext:
        raise NotImplementedError(
            "LiteLLMAdapter is a reserved Phase 3.1 interface — not implemented yet"
        )

    def extract_route_decision(self, internal_state: Any) -> Optional[RouteDecision]:
        raise NotImplementedError(
            "LiteLLMAdapter is a reserved Phase 3.1 interface — not implemented yet"
        )

    def extract_attempt_context(self, internal_state: Any) -> Optional[AttemptContext]:
        raise NotImplementedError(
            "LiteLLMAdapter is a reserved Phase 3.1 interface — not implemented yet"
        )

    def extract_usage(self, response: Any) -> Any:
        raise NotImplementedError(
            "LiteLLMAdapter is a reserved Phase 3.1 interface — not implemented yet"
        )

    def classify_error(self, error: BaseException) -> GatewayError:
        raise NotImplementedError(
            "LiteLLMAdapter is a reserved Phase 3.1 interface — not implemented yet"
        )
