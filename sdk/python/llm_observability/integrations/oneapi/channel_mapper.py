"""One-API Channel → gateway channel fields (hashed by PrivacyGuard)."""
from typing import Any, Optional

from ...gateway_observability.adapter import _get


def map_channel(channel: Any) -> dict:
    """Map a One-API Channel to {channel_id, channel_type, provider}.

    Returns the raw internal channel_id — the PrivacyGuard at the runtime
    boundary performs the hash/HMAC. This mapper never mutates the channel.
    """
    if channel is None:
        return {}
    return {
        "channel_id": _get(channel, "id") or _get(channel, "channel_id") or _get(channel, "key"),
        "channel_type": _get(channel, "type") or _get(channel, "channel_type"),
        "provider": _get(channel, "provider") or _get(channel, "name"),
    }


def map_model_mapping(mapping: Any, requested_model: Optional[str] = None) -> dict:
    """Map One-API model mapping to {requested_model, resolved_model}.

    One-API model mappings map a request model name to a provider model name.
    """
    resolved = None
    if isinstance(mapping, dict):
        if requested_model:
            resolved = mapping.get(requested_model) or mapping.get(str(requested_model))
        if resolved is None:
            # First explicit mapping entry as a fallback.
            for k, v in list(mapping.items())[:8]:
                if v and not str(k).startswith("_"):
                    resolved = v
                    break
    elif mapping is not None and hasattr(mapping, "model"):
        resolved = _get(mapping, "model")
    return {
        "requested_model": requested_model,
        "resolved_model": resolved or requested_model,
    }


def map_relay_mode(relay_mode: Any) -> str:
    """Map One-API relay mode to gateway.protocol."""
    if relay_mode is None:
        return "openai-compatible"
    value = str(relay_mode).lower()
    if value in ("chat", "openai", "openai-compatible"):
        return "openai-compatible"
    if value in ("completion", "completions"):
        return "openai-compatible"
    if value in ("anthropic", "claude"):
        return "anthropic"
    return value
