"""One-API request → GatewayRequestContext mapping (spec §19.2)."""
from typing import Any, Optional

from ...gateway_observability.context import GatewayRequestContext
from ...gateway_observability.adapter import _get


def map_request_token(request_token: Any) -> GatewayRequestContext:
    """Map a One-API request token to a GatewayRequestContext.

    One-API request tokens typically carry: ``key_id`` / ``user_id`` /
    ``session_id`` / ``app`` / ``model`` / ``channel``. Only the association
    fields that are safe (no secrets) are carried over.
    """
    return GatewayRequestContext(
        gateway_name=_get(request_token, "gateway_name", "one-api") or "one-api",
        gateway_version=_get(request_token, "gateway_version"),
        request_id=_get(request_token, "request_id"),
        protocol=_get(request_token, "protocol", "openai-compatible") or "openai-compatible",
        route=_get(request_token, "route", "/v1/chat/completions"),
        requested_model=_get(request_token, "model") or _get(request_token, "requested_model"),
        user_id=_get(request_token, "user_id"),
        session_id=_get(request_token, "session_id"),
        message_id=_get(request_token, "message_id"),
        app_name=_get(request_token, "app_name") or _get(request_token, "app"),
        business_scenario=_get(request_token, "business_scenario"),
    )


def map_request(request: Any) -> GatewayRequestContext:
    """Map a raw One-API request object to a GatewayRequestContext.

    Accepts either a request token object/dict (preferred) or a request dict
    with a ``token`` field.
    """
    if request is None:
        return GatewayRequestContext(gateway_name="one-api")
    token = _get(request, "token", request)
    return map_request_token(token)
