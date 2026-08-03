"""One-API glue-layer adapter (spec §19).

Maps One-API request token → association, Channel → gateway fields (hashed),
model mapping → requested/resolved model, relay mode → protocol, retry →
new Attempt + event, fallback → event + new Attempt, quota → usage/cost,
upstream response → Attempt result.

Boundary constraints (spec §19.3): the adapter NEVER changes channel
selection, retry counts, timeouts, quota, or swallows business exceptions.
It only extracts, maps, invokes events, and normalizes state.
"""
from .adapter import OneApiAdapter

__all__ = ["OneApiAdapter"]
