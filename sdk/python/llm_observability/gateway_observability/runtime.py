"""GatewayRuntime — orchestrates adapter → RouterSpan → AttemptSpan → recorder → reporter.

Each step is isolated in try/except so telemetry failure never changes gateway
business behavior (fail-open, spec §18). The runtime owns the Router/Attempt
registries and the sampling/privacy/usage/cost components.
"""
import logging
import time
from typing import Any, Optional

from .adapter import GatewayAdapter, GenericAdapter
from .context import GatewayRequestContext, GatewayContext
from .cost import CostCalculator
from .errors import ErrorCategory
from .privacy import PrivacyGuard
from .propagation import decide_sampling
from .registry import AttemptRegistry, RouterRegistry
from .router_span import RouterSpan, RouteDecision, AttemptResult
from .usage import NormalizedUsage, UsageNormalizer

logger = logging.getLogger("llm_obs.gateway.runtime")

GATEWAY_CACHE_HIT = "hit"
GATEWAY_CACHE_MISS = "miss"
GATEWAY_CACHE_BYPASS = "bypass"
GATEWAY_CACHE_ERROR = "error"


class GatewayRuntime:
    """Tie the adapter contract to the Router/Attempt runtime (fail-open).

    Args:
        tracer: SDK Tracer (used for sampling + reporter).
        adapter: GatewayAdapter implementation (default GenericAdapter).
        sample_rate: Local sampling rate used when no upstream traceparent.
        privacy: Optional PrivacyGuard (default-deny + channel hashing).
        usage_normalizer / cost_calculator: Optional overrides.
        router_registry / attempt_registry: Optional registries (auto-created).
    """

    def __init__(
        self,
        tracer=None,
        adapter: Optional[GatewayAdapter] = None,
        sample_rate: float = 1.0,
        privacy: Optional[PrivacyGuard] = None,
        usage_normalizer: Optional[UsageNormalizer] = None,
        cost_calculator: Optional[CostCalculator] = None,
        router_registry: Optional[RouterRegistry] = None,
        attempt_registry: Optional[AttemptRegistry] = None,
    ):
        self._tracer = tracer
        self._adapter = adapter if adapter is not None else GenericAdapter()
        self._sample_rate = sample_rate
        self._privacy = privacy if privacy is not None else PrivacyGuard()
        self._usage_normalizer = usage_normalizer if usage_normalizer is not None else UsageNormalizer()
        self._cost_calculator = cost_calculator if cost_calculator is not None else CostCalculator()
        self.router_registry = router_registry if router_registry is not None else RouterRegistry()
        self.attempt_registry = attempt_registry if attempt_registry is not None else AttemptRegistry()

    # ── entry point ──

    def handle_request(self, request: Any, upstream_traceparent: Optional[str] = None):
        """Process one gateway request end-to-end.

        Returns a ``GatewayRuntimeHandle`` holding the Router span (and the
        final result context) so the caller can start attempts and finalize.

        Fail-open: any extraction/span-creation failure still returns a handle;
        the business request path is unaffected.
        """
        sampled = self._decide_sampled(upstream_traceparent)
        request_context = None
        route_decision = None
        upstream_trace_id = None
        upstream_parent_span_id = None
        if upstream_traceparent:
            try:
                from .propagation import extract_traceparent_ids
                upstream_trace_id, upstream_parent_span_id, _ = extract_traceparent_ids(
                    upstream_traceparent
                )
            except Exception as e:
                logger.error("Gateway upstream trace extraction failed: %s", e)
        try:
            request_context = self._adapter.extract_request_context(request)
        except Exception as e:
            logger.error("Gateway request-context extraction failed: %s", e)
            request_context = GatewayRequestContext()
        try:
            route_decision = self._adapter.extract_route_decision(request)
        except Exception as e:
            logger.error("Gateway route-decision extraction failed: %s", e)
            route_decision = None

        router = RouterSpan(
            tracer=self._tracer,
            request_context=request_context,
            route_decision=route_decision,
            sampled=sampled,
            privacy=self._privacy,
            router_registry=self.router_registry,
            attempt_registry=self.attempt_registry,
            upstream_trace_id=upstream_trace_id,
            upstream_parent_span_id=upstream_parent_span_id,
        )
        router.start()
        return GatewayRuntimeHandle(self, router)

    async def handle_request_async(self, request: Any, upstream_traceparent: Optional[str] = None):
        """Async variant — same semantics as ``handle_request``."""
        return self.handle_request(request, upstream_traceparent)

    def _decide_sampled(self, upstream_traceparent: Optional[str]) -> bool:
        """Honor an upstream sampling decision; else local sample_rate."""
        try:
            return decide_sampling(upstream_traceparent, self._sample_rate)
        except Exception as e:
            logger.error("Gateway sampling decision failed (sampling in): %s", e)
            return True

    # ── per-attempt orchestration ──

    def create_attempt(self, router: RouterSpan, internal_state: Any = None) -> Any:
        """Extract an attempt context and create a fresh AttemptSpan.

        Returns the AttemptSpan, or None when extraction fails (fail-open).
        """
        try:
            ctx = self._adapter.extract_attempt_context(internal_state)
        except Exception as e:
            logger.error("Gateway attempt-context extraction failed: %s", e)
            ctx = None

        index = None
        provider = None
        channel_id = None
        channel_type = None
        resolved_model = None
        timeout_ms = None
        if ctx is not None:
            # None → Router auto-allocates; invalid values fall back inside
            # router.allocate_attempt_index (never a hardcoded 1).
            index = getattr(ctx, "attempt_index", None)
            provider = getattr(ctx, "provider", None)
            channel_id = getattr(ctx, "channel_id", None)
            channel_type = getattr(ctx, "channel_type", None)
            resolved_model = getattr(ctx, "resolved_model", None)
            timeout_ms = getattr(ctx, "timeout_ms", None)

        return router.attempt(
            attempt_index=index,
            provider=provider,
            channel_id=channel_id,
            channel_type=channel_type,
            resolved_model=resolved_model,
            timeout_ms=timeout_ms,
        )

    def finalize_attempt(self, attempt, response: Any = None, error: Optional[BaseException] = None,
                         upstream_status: Optional[int] = None,
                         duration_ms: Optional[float] = None,
                         ttft_ms: Optional[float] = None,
                         finish_reason: Optional[str] = None,
                         raw_usage: Any = None):
        """Normalize response/error → AttemptSpan fields (fail-open)."""
        if attempt is None:
            return None
        # Normalize usage (fail-open).
        normalized = None
        if raw_usage is not None:
            normalized = self._usage_normalizer.normalize(raw_usage)
        elif response is not None:
            try:
                raw = self._adapter.extract_usage(response)
                normalized = self._usage_normalizer.normalize(raw)
            except Exception as e:
                logger.error("Gateway usage extraction failed: %s", e)

        # Classify error (fail-open).
        gateway_error = None
        if error is not None:
            try:
                gateway_error = self._adapter.classify_error(error)
            except Exception as e:
                logger.error("Gateway error classification failed: %s", e)
                from .errors import classify_error as _cf
                gateway_error = _cf(error)
        elif upstream_status is not None and upstream_status >= 400:
            from .errors import classify_http_status
            category = classify_http_status(upstream_status)
            from .errors import GatewayError, is_retryable_category
            gateway_error = GatewayError(
                category=category,
                type=f"HTTP {upstream_status}",
                message=f"upstream returned HTTP {upstream_status}",
                retryable=is_retryable_category(category),
            )

        # Compute cost with the attempt's resolved model (fail-open).
        cost = self._cost_calculator.calculate(
            normalized, model=getattr(attempt, "_resolved_model", None),
        )

        if gateway_error is not None:
            attempt.set_error(gateway_error)
        attempt.set_upstream_status(status_code=upstream_status, duration_ms=duration_ms, ttft_ms=ttft_ms)
        if finish_reason is not None:
            attempt.set_finish_reason(finish_reason)
        if normalized is not None:
            attempt.set_usage(normalized)
        if cost is not None:
            attempt.set_cost(cost)

        succeeded = gateway_error is None and (upstream_status is None or upstream_status < 400)
        result = AttemptResult(
            attempt_index=attempt.attempt_index,
            channel_id=attempt.channel_id,
            http_status_code=upstream_status,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            error=gateway_error,
            finish_reason=finish_reason,
            usage=normalized,
            cost=cost,
            success=succeeded,
        )
        # Terminal lifecycle event on the attempt span (exactly once, before
        # span end — the caller closes the attempt after this returns).
        try:
            attempt._terminal_event_recorded = True
            if succeeded:
                attempt.recorder.attempt_completed(
                    attempt_index=attempt.attempt_index, http_status_code=upstream_status,
                )
            else:
                attempt.recorder.attempt_failed(
                    attempt_index=attempt.attempt_index,
                    error_category=gateway_error.category if gateway_error is not None else None,
                    http_status_code=upstream_status,
                )
        except Exception as e:
            logger.error("Gateway attempt terminal event failed: %s", e)
        # Aggregate into the Router (including failed attempts), at most once
        # per attempt — a streaming wrapper must not re-aggregate a finalize
        # that already happened here. try_aggregate_result makes the
        # _aggregated_to_router check-and-set atomic under _lifecycle_lock so
        # a racing force_close/streaming-finalizer cannot double-count.
        try:
            attempt.try_aggregate_result(result)
        except Exception as e:
            logger.error("Router attempt aggregation failed: %s", e)
        return result

    def finalize_router(self, router: Optional[RouterSpan]):
        """End the Router span at a terminal state (fail-open)."""
        if router is None:
            return
        try:
            router.close()
        except Exception as e:
            logger.error("Router finalize failed: %s", e)
        # Guarantee no residual registry/ContextVar even on finalize failure.
        try:
            from .context import clear_gateway_context
            from .context import GatewayContext
            state = GatewayContext.get()
            if state.router is None:
                clear_gateway_context()
        except Exception:
            pass

    # ── cache / rate-limit shortcuts ──

    def handle_cache(self, router: Optional[RouterSpan], cache_status: str = GATEWAY_CACHE_HIT,
                     usage: Any = None, cost: Any = None):
        """Record a cache decision. A hit creates no Attempt (spec §14.1).

        An explicit caller-provided cost is preserved; otherwise cost is
        computed from usage with the router's resolved model (unpriced when
        the model has no pricing entry).
        """
        if router is None:
            return
        router.set_cache_status(cache_status)
        if cache_status == GATEWAY_CACHE_HIT:
            normalized = None
            if usage is not None:
                normalized = self._usage_normalizer.normalize(usage)
            if normalized is not None:
                router.set_usage_aggregate(normalized)
                if cost is not None:
                    calc = cost  # explicit caller cost wins
                else:
                    resolved_model = None
                    try:
                        rd = getattr(router, "_route_decision", None)
                        resolved_model = getattr(rd, "resolved_model", None)
                    except Exception:
                        resolved_model = None
                    calc = self._cost_calculator.calculate(normalized, model=resolved_model)
                if calc is not None:
                    router.set_cost_aggregate(calc)

    def handle_rate_limit(self, router: Optional[RouterSpan]):
        """Record a rate-limit rejection: Router ERROR, attempt_count=0."""
        if router is None:
            return
        router.set_rate_limited()

    # ── context access ──

    def active_router(self) -> Optional[RouterSpan]:
        """Current Router from the gateway ContextVar (or None)."""
        state = GatewayContext.get()
        return state.router if state is not None else None

    def active_attempt(self):
        """Current Attempt from the gateway ContextVar (or None).

        Never returns an ended Attempt: ``GatewayContext.get()`` lazily
        invalidates the slot when the referent is dead/closed, so this reads
        ``None`` for any ended Attempt (cross-thread force_close included).
        """
        state = GatewayContext.get()
        if state is None or state.active_attempt is None:
            return None
        return state.active_attempt.attempt()


class GatewayRuntimeHandle:
    """Handle returned by GatewayRuntime.handle_request.

    Holds the Router span and exposes the runtime for attempt orchestration,
    cache/rate-limit shortcuts, and finalization. Closing the handle is
    fail-open and idempotent.
    """

    def __init__(self, runtime: GatewayRuntime, router: RouterSpan):
        self._runtime = runtime
        self.router = router
        self._closed = False

    # ── attempt lifecycle ──

    def start_attempt(self, internal_state: Any = None):
        """Create a fresh AttemptSpan for one real upstream request."""
        if self.router is None:
            return None
        return self._runtime.create_attempt(self.router, internal_state)

    def finish_attempt(self, attempt, response: Any = None, error: Optional[BaseException] = None,
                       upstream_status: Optional[int] = None, duration_ms: Optional[float] = None,
                       ttft_ms: Optional[float] = None, finish_reason: Optional[str] = None,
                       raw_usage: Any = None):
        """Normalize + aggregate one attempt's outcome (NON-streaming only).

        Streaming attempts must go through ``finalize_streaming_attempt`` /
        the stream wrapper's terminal funnel — never finished at header time.
        """
        return self._runtime.finalize_attempt(
            attempt, response=response, error=error, upstream_status=upstream_status,
            duration_ms=duration_ms, ttft_ms=ttft_ms, finish_reason=finish_reason,
            raw_usage=raw_usage,
        )

    # Explicit non-streaming alias so call sites state their intent.
    def finish_non_streaming_attempt(self, attempt, **kwargs):
        """Non-streaming attempt finalization (same as ``finish_attempt``)."""
        return self.finish_attempt(attempt, **kwargs)

    def finalize_streaming_attempt(self, iterable, attempt, check_done: bool = True,
                                   async_stream: bool = False,
                                   upstream_status: Optional[int] = None,
                                   duration_ms: Optional[float] = None,
                                   connect_duration_ms: Optional[float] = None):
        """Wrap an upstream stream for terminal-state finalization.

        The Attempt stays open until the stream reaches a terminal state
        (full consumption / [DONE] / cancel / error); success is NEVER
        aggregated at header time. Header-time upstream facts (status /
        duration) are recorded on the Attempt but do not finalize it.
        Returns the wrapped stream.
        """
        from .streaming import wrap_stream, wrap_async_stream
        if async_stream:
            return wrap_async_stream(iterable, self.router, attempt,
                                     runtime_handle=self, check_done=check_done,
                                     upstream_status=upstream_status,
                                     duration_ms=duration_ms,
                                     connect_duration_ms=connect_duration_ms)
        return wrap_stream(iterable, self.router, attempt,
                           runtime_handle=self, check_done=check_done,
                           upstream_status=upstream_status,
                           duration_ms=duration_ms,
                           connect_duration_ms=connect_duration_ms)

    def cache_hit(self, usage: Any = None, cost: Any = None):
        """Record a cache hit (no Attempt created)."""
        self._runtime.handle_cache(self.router, GATEWAY_CACHE_HIT, usage, cost)

    def cache_miss(self):
        """Record a cache miss (proceed to attempts)."""
        self._runtime.handle_cache(self.router, GATEWAY_CACHE_MISS)

    def cache_bypass(self):
        """Record a cache bypass."""
        self._runtime.handle_cache(self.router, GATEWAY_CACHE_BYPASS)

    def rate_limited(self):
        """Record a rate-limit rejection (Router ERROR, no Attempt)."""
        self._runtime.handle_rate_limit(self.router)

    def retry_scheduled(self, attempt_index=None, delay_ms=None, reason=None):
        """Record a retry decision (fresh Attempt created next)."""
        if self.router is None:
            return False
        return self.router.retry_scheduled(attempt_index=attempt_index, delay_ms=delay_ms, reason=reason)

    def fallback_selected(self, from_channel_id=None, to_channel_id=None, reason=None):
        """Record a fallback transition (fresh Attempt created next)."""
        if self.router is None:
            return False
        return self.router.fallback_selected(
            from_channel_id=from_channel_id, to_channel_id=to_channel_id, reason=reason
        )

    def finalize(self):
        """End the Router span + clean all registries/context (fail-open).

        Router.close() force-closes any still-open Attempt (spans are always
        ended — never just dropped from the registry).
        """
        if self._closed:
            return
        self._closed = True
        self._runtime.finalize_router(self.router)

    def close(self):
        self.finalize()

    # ── properties ──

    @property
    def router_span(self):
        return self.router.span if self.router is not None else None

    @property
    def sampled(self) -> bool:
        return bool(self.router._sampled) if self.router is not None else True
