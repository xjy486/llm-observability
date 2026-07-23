"""
Payload masking and privacy processing.

Strategies:
- off: No payload captured
- metadata_only: Only structural metadata (model, token counts, status), no content
- masked: Content captured but sensitive fields masked
- full: Full content captured as-is
"""
import re
import json
from typing import Any, Optional
from config import ProxyConfig


def mask_value(val: str, patterns: list) -> str:
    """Apply regex masking patterns to a string value."""
    for pattern in patterns:
        val = re.sub(pattern, "***MASKED***", val, flags=re.IGNORECASE)
    return val


def mask_object(obj: Any, patterns: list) -> Any:
    """Recursively mask sensitive values in a nested object."""
    if isinstance(obj, str):
        return mask_value(obj, patterns)
    elif isinstance(obj, dict):
        return {k: mask_object(v, patterns) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [mask_object(item, patterns) for item in obj]
    return obj


def process_payload(
    data: Optional[dict],
    strategy: str,
    config: ProxyConfig,
) -> Optional[dict]:
    """Process payload according to the configured strategy.

    Args:
        data: The raw payload dict (request body or response body)
        strategy: off / metadata_only / masked / full
        config: Proxy configuration

    Returns:
        Processed payload dict or None
    """
    if strategy == "off" or data is None:
        return None

    if strategy == "metadata_only":
        # Only keep structural metadata, strip content
        meta = {}
        if "model" in data:
            meta["model"] = data["model"]
        if "stream" in data:
            meta["stream"] = data["stream"]
        if "tools" in data:
            meta["tools_count"] = len(data["tools"]) if isinstance(data["tools"], list) else 0
        # For response-like objects
        if "usage" in data:
            meta["usage"] = data["usage"]
        if "choices" in data:
            meta["choices_count"] = len(data["choices"])
            # Keep finish_reason but not content
            meta["finish_reasons"] = [
                c.get("finish_reason") for c in data.get("choices", [])
            ]
        return meta

    if strategy == "masked":
        return mask_object(data, config.mask_patterns)

    # full
    return data


def extract_request_metadata(body: dict) -> dict:
    """Extract non-sensitive metadata from request body."""
    meta = {
        "model": body.get("model", "unknown"),
        "stream": body.get("stream", False),
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_tokens"),
    }
    tools = body.get("tools")
    if tools:
        meta["tools_count"] = len(tools)
    messages = body.get("messages")
    if messages and isinstance(messages, list):
        meta["message_count"] = len(messages)
        # Extract roles for structure preview
        meta["message_roles"] = [m.get("role", "unknown") for m in messages]
    return {k: v for k, v in meta.items() if v is not None}


def extract_response_metadata(status: int, response_body: Optional[dict], chunks: list) -> dict:
    """Extract metadata from response.

    For streaming: aggregate from chunks.
    For non-streaming: extract from response body.
    """
    meta: dict = {
        "http_status": status,
    }

    if response_body:
        # Non-streaming response
        usage = response_body.get("usage", {})
        if usage:
            meta["input_tokens"] = usage.get("prompt_tokens", 0)
            meta["output_tokens"] = usage.get("completion_tokens", 0)
            meta["total_tokens"] = usage.get("total_tokens", 0)
        model = response_body.get("model")
        if model:
            meta["response_model"] = model
        choices = response_body.get("choices", [])
        if choices:
            meta["finish_reason"] = choices[0].get("finish_reason")
    elif chunks:
        # Streaming response — aggregate from SSE chunks
        usage = None
        model = None
        finish_reason = None
        for chunk in chunks:
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("model"):
                model = chunk["model"]
            choices = chunk.get("choices", [])
            if choices and choices[0].get("finish_reason"):
                finish_reason = choices[0]["finish_reason"]

        if usage:
            meta["input_tokens"] = usage.get("prompt_tokens", 0)
            meta["output_tokens"] = usage.get("completion_tokens", 0)
            meta["total_tokens"] = usage.get("total_tokens", 0)
        else:
            meta["input_tokens"] = None
            meta["output_tokens"] = None
            meta["total_tokens"] = None
            meta["token_note"] = "unknown"
        if model:
            meta["response_model"] = model
        if finish_reason:
            meta["finish_reason"] = finish_reason

    return meta
