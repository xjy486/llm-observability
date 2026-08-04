"""AttemptSpan — one real upstream request as a GATEWAY span (spec §4, §9.3).

Parent is always the Router span. Records ``gateway.attempt_index``, upstream
status/duration/ttft, error fields, finish reason, and retryable. Its lifecycle
is managed by the Router (or the streaming wrapper) so it ends only at a
terminal state.
"""
import logging
import time
from typing import Optional

from ..spans import Span, SpanKind
from ..utils.ids import generate_span_id
from .attributes import ATTR_ATTEMPT, ATTR_GATEWAY, ATTR_USAGE, ATTR_COST, PROVIDER_ATTEMPT
from .context import GatewayContext
from .errors import GatewayError
from .privacy import PrivacyGuard, set_gateway_attribute
from .registry import AttemptRegistry
from .usage import NormalizedUsage, usage_to_attributes
from .cost import NormalizedCost, cost_to_attributes

logger = logging.getLogger("llm_obs.gateway.attempt")

_INTERNAL_STATE = {"attempt_index": 0, "provider": None, "channel_id": None,
                   "channel_type": None, "resolved_model": None, "timeout_ms": None}


class AttemptSpan:
    """Context manager for a single upstream Attempt GATEWAY span.

    Parent = the owning Router span. ``gateway.span_role = provider_attempt``.
    The span stays open until ``close``/``__exit__`` (a terminal state) —
    never at response-header/first-token/StreamingResponse return.

    Args:
        router: The owning RouterSpan.
        attempt_index: 1-based attempt index.
        provider / channel_id / channel_type / resolved_model / timeout_ms:
            AttemptContext fields.
        privacy: Optional PrivacyGuard for channel-ID hashing.
        registry: Optional AttemptRegistry to register/clean this attempt.
        tracer: Optional SDK Tracer (used only to honor sampling on report).
        sampled: Whether this attempt reports to the backend.
    """

    def __init__(
        self,
        router,
        attempt_index: int = 1,
        provider: Optional[str] = None,
        channel_id: Optional[str] = None,
        channel_type: Optional[str] = None,
        resolved_model: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        privacy: Optional[PrivacyGuard] = None,
        registry: Optional[AttemptRegistry] = None,
        tracer=None,
        sampled: bool = True,
    ):
        self._router = router
        self._attempt_index = attempt_index
        self._provider = provider
        self._channel_id = channel_id
        self._channel_type = channel_type
        self._resolved_model = resolved_model
        self._timeout_ms = timeout_ms
        self._privacy = privacy or PrivacyGuard()
        self._registry = registry
        self._tracer = tracer
        self._sampled = sampled

        self._span: Optional[Span] = None
        self._token = None
        self._ctx_token = None
        self._closed = False
        self._started_at: float = 0.0
        self._status: Optional[int] = None
        self._duration_ms: Optional[float] = None
        self._connect_duration_ms: Optional[float] = None
        self._ttft_ms: Optional[float] = None
        self._error: Optional[GatewayError] = None
        self._finish_reason: Optional[str] = None
        self._retryable: bool = False
        self._usage: Optional[NormalizedUsage] = None
        self._cost: Optional[NormalizedCost] = None
        self._upstream_request_id: Optional[str] = None
        self._terminal_event_recorded = False
        # True once this attempt's result has been aggregated into its Router
        # (via runtime.finalize_attempt OR the stream terminal finalizer).
        # Guards against double aggregation when a caller both finalizes an
        # attempt and then lets a streaming wrapper terminate it.
        self._aggregated_to_router = False
        # The raw (pre-hash) channel ID is only stored for aggregation, never
        # written to telemetry (the span carries the hashed value).
        self._raw_channel_id: Optional[str] = channel_id

        from .recorder import GatewayEventRecorder
        self.recorder = GatewayEventRecorder(span=None, privacy=self._privacy)

    def _set_attr(self, span, key, value):
        """Write an external-string span attribute through the unified guard.

        External strings (provider, resolved_model, channel_type,
        upstream_request_id, error_type, error_message, finish_reason) MUST go
        through this so the PrivacyGuard applies whitelist + secret masking +
        length limits. Internal counters, booleans, hashed channel IDs, and
        numeric metrics are written directly elsewhere.
        """
        return set_gateway_attribute(span, key, value, self._privacy)

    # ── lifecycle ──

    def start(self) -> "AttemptSpan":
        """Create and start the Attempt span (fail-open)."""
        try:
            trace_id, parent_span_id = self._parent_ids()
            span_id = generate_span_id()
            span = Span(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                span_name=f"gateway.attempt.{self._attempt_index}",
                span_kind=SpanKind.GATEWAY,
            )
            span.set_attribute(ATTR_GATEWAY["span_role"], PROVIDER_ATTEMPT)
            span.set_attribute(ATTR_ATTEMPT["attempt_index"], self._attempt_index)
            if self._provider:
                self._set_attr(span, ATTR_ATTEMPT["provider"], self._provider)
            hashed = self._privacy.hash_channel_id(self._channel_id)
            if hashed:
                span.set_attribute(ATTR_ATTEMPT["channel_id"], hashed)
            if self._channel_type:
                self._set_attr(span, ATTR_ATTEMPT["channel_type"], self._channel_type)
            if self._resolved_model:
                self._set_attr(span, ATTR_ATTEMPT["resolved_model"], self._resolved_model)
            if self._timeout_ms is not None:
                span.set_attribute(ATTR_ATTEMPT["timeout_ms"], self._timeout_ms)
            span.start()
            self._span = span
            self._started_at = span.start_time
            self.recorder.bind(span)

            # Register + track as open on the Router + attach as active attempt.
            if self._registry is not None:
                self._registry.register(trace_id, span_id, self)
            try:
                if self._router is not None:
                    self._router.register_open_attempt(self)
            except Exception as e:
                logger.error("Attempt open-register on router failed: %s", e)
            self._ctx_token = GatewayContext.set_attempt(self)
            # Lifecycle event: exactly one gateway.attempt.started per attempt.
            self.recorder.attempt_started(
                attempt_index=self._attempt_index,
                channel_id=self._raw_channel_id,
                provider=self._provider,
                resolved_model=self._resolved_model,
            )
        except Exception as e:
            logger.error("Attempt span start failed: %s", e)
        return self

    def _parent_ids(self):
        """Return (trace_id, parent_span_id) — the Attempt's parent is always
        its Router. When the Router span exists but somehow has no valid trace
        ID, a fresh valid one is generated (never None)."""
        try:
            router_span = self._router.span if self._router is not None else None
            if router_span is not None:
                trace_id = router_span.trace_id
                if trace_id:
                    return trace_id, router_span.span_id
        except Exception:
            pass
        # Fallback: current SDK context.
        try:
            from ..context import get_current_context
            ctx = get_current_context()
            if ctx is not None and getattr(ctx, "trace_id", None):
                return ctx.trace_id, ctx.span_id
        except Exception:
            pass
        # Last resort: the Attempt still must belong to a valid trace.
        from ..utils.ids import generate_trace_id
        return generate_trace_id(), None

    def set_upstream_status(
        self,
        status_code: Optional[int] = None,
        duration_ms: Optional[float] = None,
        connect_duration_ms: Optional[float] = None,
        ttft_ms: Optional[float] = None,
    ) -> "AttemptSpan":
        """Record upstream response facts (fail-open)."""
        self._status = status_code
        self._duration_ms = duration_ms
        self._connect_duration_ms = connect_duration_ms
        self._ttft_ms = ttft_ms
        try:
            if self._span is not None:
                if status_code is not None:
                    self._span.set_attribute(ATTR_ATTEMPT["upstream_http_status_code"], status_code)
                if duration_ms is not None:
                    self._span.set_attribute(ATTR_ATTEMPT["upstream_duration_ms"], duration_ms)
                if connect_duration_ms is not None:
                    self._span.set_attribute(ATTR_ATTEMPT["upstream_connect_duration_ms"], connect_duration_ms)
                if ttft_ms is not None:
                    self._span.set_attribute(ATTR_ATTEMPT["upstream_ttft_ms"], ttft_ms)
        except Exception as e:
            logger.error("Attempt upstream status failed: %s", e)
        return self

    def set_upstream_request_id(self, request_id: Optional[str]) -> "AttemptSpan":
        """Record the upstream request ID (fail-open)."""
        self._upstream_request_id = request_id
        try:
            if self._span is not None and request_id:
                self._set_attr(span=self._span, key=ATTR_ATTEMPT["upstream_request_id"], value=request_id)
        except Exception as e:
            logger.error("Attempt upstream request id failed: %s", e)
        return self

    def set_error(self, error: GatewayError) -> "AttemptSpan":
        """Record a classified failure (fail-open)."""
        self._error = error
        self._retryable = bool(error.retryable)
        try:
            if self._span is not None:
                span = self._span
                span.set_attribute(ATTR_ATTEMPT["error_category"], error.category)
                span.set_attribute(ATTR_ATTEMPT["retryable"], bool(error.retryable))
                if error.type:
                    self._set_attr(span, ATTR_ATTEMPT["error_type"], error.type)
                if error.message:
                    self._set_attr(span, ATTR_ATTEMPT["error_message"], error.message)
        except Exception as e:
            logger.error("Attempt set_error failed: %s", e)
        return self

    def set_finish_reason(self, finish_reason: Optional[str]) -> "AttemptSpan":
        """Record the upstream finish reason (fail-open)."""
        self._finish_reason = finish_reason
        try:
            if self._span is not None and finish_reason:
                self._set_attr(span=self._span, key=ATTR_ATTEMPT["finish_reason"], value=finish_reason)
        except Exception as e:
            logger.error("Attempt finish reason failed: %s", e)
        return self

    def set_usage(self, usage: Optional[NormalizedUsage]) -> "AttemptSpan":
        """Record this attempt's usage (fail-open)."""
        self._usage = usage
        try:
            if self._span is not None:
                for key, value in usage_to_attributes(usage).items():
                    self._span.set_attribute(key, value)
        except Exception as e:
            logger.error("Attempt usage failed: %s", e)
        return self

    def set_cost(self, cost: Optional[NormalizedCost]) -> "AttemptSpan":
        """Record this attempt's cost (fail-open)."""
        self._cost = cost
        try:
            if self._span is not None:
                for key, value in cost_to_attributes(cost).items():
                    self._span.set_attribute(key, value)
        except Exception as e:
            logger.error("Attempt cost failed: %s", e)
        return self

    def _record_terminal_event(self):
        """Fallback terminal event when none was recorded before span end.

        ``GatewayRuntime.finalize_attempt`` records the canonical event; this
        catches attempts closed without a finalize call (e.g. context-manager
        exit with an exception).
        """
        try:
            if self._terminal_event_recorded:
                return
            self._terminal_event_recorded = True
            if self._error is not None:
                self.recorder.attempt_failed(
                    attempt_index=self._attempt_index,
                    error_category=self._error.category,
                    http_status_code=self._status,
                )
            else:
                self.recorder.attempt_completed(
                    attempt_index=self._attempt_index,
                    http_status_code=self._status,
                )
        except Exception as e:
            logger.error("Attempt terminal event failed: %s", e)

    def close(self):
        """End the Attempt span at a terminal state (fail-open).

        Registry entry + active-attempt ContextVar are always cleaned, even if
        span end or report raises.
        """
        if self._closed:
            return
        self._closed = True
        try:
            try:
                if self._span is not None:
                    if self._duration_ms is None and self._started_at:
                        self._duration_ms = round((time.time() - self._started_at) * 1000, 2)
                    if self._error is not None:
                        self._span.set_status("ERROR")
                    else:
                        self._span.set_status("OK")
                    if self._duration_ms is not None:
                        self._span.set_attribute(ATTR_ATTEMPT["upstream_duration_ms"], self._duration_ms)
                    self._record_terminal_event()
                    self._span.end()
                    if self._sampled and self._tracer is not None:
                        try:
                            self._tracer.reporter.report(self._span.to_record())
                        except Exception as e:
                            logger.error("Failed to report Attempt span: %s", e)
            except Exception as e:
                logger.error("Attempt span end failed: %s", e)
        finally:
            self._cleanup()

    def force_close(self, category: str = "gateway_internal",
                    reason: str = "router_finalized_with_open_attempt"):
        """Force-close this Attempt when the Router finalizes with it open.

        Idempotent (a closed Attempt is a no-op), fail-open, never overwrites
        an already-recorded business error, cleans the registry/context, and
        reports the final span. The force-close is the single funnel that also
        constructs the terminal ``AttemptResult`` and aggregates it into the
        Router exactly once (via ``_aggregated_to_router``) — so a Router that
        finalizes with an open Attempt ends ERROR with the right
        ``final_error_category`` and ``fail_count``, never OK while its child
        Attempt is ERROR.
        """
        if self._closed:
            # Already closed (possibly already aggregated by finalize_attempt /
            # the streaming finalizer). Idempotent — do not re-aggregate.
            return
        try:
            if self._error is None:
                from .errors import GatewayError
                self.set_error(GatewayError(
                    category=category,
                    type="GatewayInternalError",
                    message=reason,
                    retryable=False,
                ))
        except Exception as e:
            logger.error("Attempt force_close error-mark failed: %s", e)
        # Aggregate the terminal result into the Router exactly once. A
        # business error already on the Attempt is preserved (force-close only
        # set gateway_internal when no error existed). Captured usage/cost
        # (partial consumption) are carried into the result.
        self._aggregate_force_close_result()
        self.close()

    def _aggregate_force_close_result(self):
        """Build the terminal AttemptResult and aggregate it to the Router.

        Idempotent via ``_aggregated_to_router``; fail-open. Mirrors the
        streaming finalizer / runtime.finalize_attempt aggregation so the
        Router's fail_count, final_error and cost/usage aggregates stay
        consistent with this force-closed Attempt.
        """
        try:
            if self._router is None or self._aggregated_to_router:
                return
            from .router_span import AttemptResult
            error = self._error
            result = AttemptResult(
                attempt_index=self._attempt_index,
                channel_id=self._raw_channel_id,
                http_status_code=self._status,
                duration_ms=self._duration_ms,
                ttft_ms=self._ttft_ms,
                error=error,
                finish_reason=self._finish_reason,
                usage=self._usage,
                cost=self._cost,
                success=False,
            )
            self._aggregated_to_router = True
            self._router.register_attempt_result(result)
        except Exception as e:
            logger.error("Attempt force_close aggregation failed: %s", e)

    def _cleanup(self):
        """Unregister (Router open-attempts + registry) + clear ONLY the
        active-attempt ContextVar slot — the Router slot always survives an
        Attempt close (fail-open)."""
        try:
            if self._router is not None:
                self._router.unregister_open_attempt(self)
        except Exception as e:
            logger.error("Attempt router unregister failed: %s", e)
        try:
            if self._registry is not None and self._span is not None:
                self._registry.remove(self._span.trace_id, self._span.span_id)
        except Exception as e:
            logger.error("Attempt registry cleanup failed: %s", e)
        try:
            if self._ctx_token is not None:
                GatewayContext.clear_attempt(self._ctx_token)
                self._ctx_token = None
        except Exception as e:
            logger.error("Attempt context cleanup failed: %s", e)
        # Belt-and-braces: clear only the attempt slot if it still points at
        # this attempt (e.g. the token belonged to another asyncio Context).
        try:
            current = GatewayContext.get()
            if current.active_attempt is self:
                GatewayContext.clear_attempt_only()
        except Exception:
            pass

    def __enter__(self) -> "AttemptSpan":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and self._error is None:
            from .errors import classify_error
            self.set_error(classify_error(exc_val))
        self.close()
        return False  # do not suppress business exceptions

    # ── properties ──

    @property
    def span(self) -> Optional[Span]:
        return self._span

    @property
    def attempt_index(self) -> int:
        return self._attempt_index

    @property
    def status(self) -> Optional[int]:
        return self._status

    @property
    def duration_ms(self) -> Optional[float]:
        return self._duration_ms

    @property
    def ttft_ms(self) -> Optional[float]:
        return self._ttft_ms

    @property
    def error(self) -> Optional[GatewayError]:
        return self._error

    @property
    def retryable(self) -> bool:
        return self._retryable

    @property
    def usage(self) -> Optional[NormalizedUsage]:
        return self._usage

    @property
    def cost(self) -> Optional[NormalizedCost]:
        return self._cost

    @property
    def finish_reason(self) -> Optional[str]:
        return self._finish_reason

    @property
    def success(self) -> bool:
        return self._error is None and (self._status is None or self._status < 400)

    @property
    def provider(self) -> Optional[str]:
        return self._provider

    @property
    def channel_id(self) -> Optional[str]:
        """Raw (internal) channel ID for Router aggregation.

        NEVER written to telemetry — the span carries the hashed value.
        """
        return self._raw_channel_id
