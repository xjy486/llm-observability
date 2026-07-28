"""Helpers for capturing Runnable root input/output.

Reuses safe_serialize, mask_payload, apply_size_guard from the SDK.
"""
import logging
from typing import Any

logger = logging.getLogger("llm_obs.integrations.langchain.runnable_metadata")


def capture_root_payload(
    value: Any,
    payload_strategy: str,
    label: str,
) -> dict:
    """Capture and guard a root input or output payload.

    Args:
        value: The input or output value.
        payload_strategy: off/metadata_only/masked/full.
        label: 'input' or 'output'.

    Returns:
        Dict with attributes and optional payload data.
    """
    from ...tool import safe_serialize, apply_size_guard
    from ...utils.masking import mask_payload

    result = {}
    try:
        if value is None:
            result[f"runnable.{label}.type"] = "None"
            result[f"runnable.{label}.size_bytes"] = 0
            result[f"runnable.{label}.truncated"] = False
            return result

        if payload_strategy == "off":
            result[f"runnable.{label}.type"] = type(value).__name__
            return result

        serialized = safe_serialize(value)
        masked = mask_payload(serialized, payload_strategy)
        guarded, truncated, orig_size = apply_size_guard(masked)

        result[f"runnable.{label}.type"] = type(value).__name__
        result[f"runnable.{label}.size_bytes"] = orig_size
        result[f"runnable.{label}.truncated"] = truncated
        result["_payload"] = {label: guarded}
    except Exception as e:
        logger.debug("capture_root_payload failed: %s", e)
        result[f"runnable.{label}.type"] = "<error>"

    return result
