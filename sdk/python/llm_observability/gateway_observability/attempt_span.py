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
from .privacy import PrivacyGuard
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
        # The raw (pre-hash) channel ID is only stored for aggregation, never
        # written to telemetry (the span carries the hashed value).
        self._raw_channel_id: Optional[str] = channel_id

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
                span.set_attribute(ATTR_ATTEMPT["provider"], self._provider)
            hashed = self._privacy.hash_channel_id(self._channel_id)
            if hashed:
                span.set_attribute(ATTR_ATTEMPT["channel_id"], hashed)
            if self._channel_type:
                span.set_attribute(ATTR_ATTEMPT["channel_type"], self._channel_type)
            if self._resolved_model:
                span.set_attribute(ATTR_ATTEMPT["resolved_model"], self._resolved_model)
            if self._timeout_ms is not None:
                span.set_attribute(ATTR_ATTEMPT["timeout_ms"], self._timeout_ms)
            span.start()
            self._span = span
            self._started_at = span.start_time

            # Register + attach as the active attempt.
            if self._registry is not None:
                self._registry.register(trace_id, span_id, self)
            self._ctx_token = GatewayContext.set_attempt(self)
        except Exception as e:
            logger.error("Attempt span start failed: %s", e)
        return self

    def _parent_ids(self):
        try:
            router_span = self._router.span if self._router is not None else None
            if router_span is not None:
                return router_span.trace_id, router_span.span_id
        except Exception:
            pass
        # Fallback: current SDK context.
        try:
            from ..context import get_current_context
            ctx = get_current_context()
            if ctx is not None:
                return ctx.trace_id, ctx.span_id
        except Exception:
            pass
        return None, None

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
                self._span.set_attribute(ATTR_ATTEMPT["upstream_request_id"], request_id)
        except Exception as e:
            logger.error("Attempt upstream request id failed: %s", e)
        return self

    def set_error(self, error: GatewayError) -> "AttemptSpan":
        """Record a classified failure (fail-open)."""
        self._error = error
        self._retryable = bool(error.retryable)
        try:
            if self._span is not None:
                self._span.set_attribute(ATTR_ATTEMPT["error_category"], error.category)
                self._span.set_attribute(ATTR_ATTEMPT["retryable"], bool(error.retryable))
                if error.type:
                    self._span.set_attribute(ATTR_ATTEMPT["error_type"], error.type)
                if error.message:
                    self._span.set_attribute(ATTR_ATTEMPT["error_message"], error.message)
        except Exception as e:
            logger.error("Attempt set_error failed: %s", e)
        return self

    def set_finish_reason(self, finish_reason: Optional[str]) -> "AttemptSpan":
        """Record the upstream finish reason (fail-open)."""
        self._finish_reason = finish_reason
        try:
            if self._span is not None and finish_reason:
                self._span.set_attribute(ATTR_ATTEMPT["finish_reason"], finish_reason)
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

    def _cleanup(self):
        """Unregister + clear the active-attempt ContextVar (fail-open)."""
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
        # Force-clear any stale active-attempt slot even cross-Context.
        try:
            current = GatewayContext.get()
            if current.active_attempt is self or current.active_attempt is None:
                from .context import clear_gateway_context
                clear_gateway_context()
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
