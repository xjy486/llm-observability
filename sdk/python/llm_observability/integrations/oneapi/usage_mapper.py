"""One-API quota/usage mapping (spec §19.2 → Usage/Cost)."""
from typing import Any, Optional

from ...gateway_observability.usage import NormalizedUsage, UsageNormalizer, _as_int


def map_quota_to_usage(quota: Any, source: str = "one-api") -> Optional[NormalizedUsage]:
    """Map One-API quota data into a NormalizedUsage.

    One-API quota payloads carry prompt/completion/total token counts under a
    variety of keys; we normalize the common OpenAI-compatible shapes.
    """
    normalizer = UsageNormalizer(source=source)
    if quota is None:
        return None
    if isinstance(quota, dict):
        normalized = normalizer.normalize(quota, source=source)
        if normalized is not None:
            return normalized
        # One-API alt keys.
        alt = {
            "prompt_tokens": _as_int(quota.get("prompt_tokens") or quota.get("input_tokens")),
            "completion_tokens": _as_int(quota.get("completion_tokens") or quota.get("output_tokens")),
            "total_tokens": _as_int(quota.get("total_tokens")),
        }
        return normalizer.normalize(alt, source=source)
    return None
