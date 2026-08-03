"""One-API upstream response → Attempt result mapping (spec §19.2)."""
from typing import Any, Optional

from ...gateway_observability.adapter import _get


def map_upstream_response(response: Any) -> dict:
    """Map an upstream response into the Attempt result fields.

    Returns:
        {http_status_code, duration_ms, ttft_ms, finish_reason, usage, request_id}
    """
    usage = None
    if isinstance(response, dict):
        usage = response.get("usage")
    elif response is not None:
        usage = getattr(response, "usage", None)
    finish_reason = None
    try:
        if isinstance(response, dict):
            choices = response.get("choices") or []
            if choices:
                finish_reason = choices[0].get("finish_reason")
        else:
            finish_reason = getattr(response, "finish_reason", None)
            if finish_reason is None and getattr(response, "choices", None):
                finish_reason = getattr(response.choices[0], "finish_reason", None)
    except Exception:
        pass
    return {
        "http_status_code": _get(response, "status_code") if isinstance(response, dict) else getattr(response, "status_code", None),
        "duration_ms": _get(response, "duration_ms") if isinstance(response, dict) else getattr(response, "duration_ms", None),
        "ttft_ms": _get(response, "ttft_ms") if isinstance(response, dict) else getattr(response, "ttft_ms", None),
        "finish_reason": finish_reason,
        "usage": usage,
        "request_id": _get(response, "request_id") if isinstance(response, dict) else getattr(response, "request_id", None),
    }


def map_error_response(error: BaseException, http_status_code: Optional[int] = None) -> dict:
    """Map a One-API upstream error into a GatewayError input.

    Returns {error, http_status_code} for the runtime to classify.
    """
    return {
        "error": error,
        "http_status_code": http_status_code,
    }
