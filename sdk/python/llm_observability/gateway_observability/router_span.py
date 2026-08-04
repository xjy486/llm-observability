"""RouterSpan — the Router GATEWAY span owning the logical gateway decision.

Router is a child of the SDK LLM span when present, else the Root. Records the
route decision, retry/fallback/attempt counts, final channel, duration metrics,
and the aggregate Usage/Cost across all attempts (including failed ones).

Also exposes ``attempt(...)`` (creates a fresh AttemptSpan per real upstream
request) and the retry/fallback/cache/rate-limit decision methods (spec §13-14).

Data models: ``RouteDecision`` (spec §8.2), ``AttemptContext`` (spec §8.3),
and ``AttemptResult`` (internal aggregation unit).
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..spans import Span, SpanKind
from ..utils.ids import generate_span_id, generate_trace_id
from .attributes import ATTR_GATEWAY, ATTR_ROUTER, ATTR_USAGE, ATTR_COST, ROUTER
from .attempt_span import AttemptSpan
from .context import GatewayContext
from .cost import NormalizedCost, add_cost, cost_to_attributes
from .errors import GatewayError, ErrorCategory
from .privacy import PrivacyGuard
from .recorder import GatewayEventRecorder
from .registry import AttemptRegistry, RouterRegistry
from .usage import NormalizedUsage, add_usage, usage_to_attributes

logger = logging.getLogger("llm_obs.gateway.router")


@dataclass
class RouteDecision:
    """Result of gateway routing for one request (spec §8.2).

    Attributes:
        provider: Resolved upstream provider name.
        channel_id: Internal channel ID (hashed before recording).
        channel_type: Channel type (e.g. 'openai', 'azure', 'anthropic').
        requested_model: Model name as requested by the caller.
        resolved_model: Model name after gateway model mapping.
        route_reason: Why this channel was chosen.
        policy_name: Optional routing policy / degradation policy name.
        fallback_from_channel_id: Source channel when this is a fallback move.
        retryable: Whether the routing decision is retryable.
        cache_status: 'hit' | 'miss' | 'bypass' | 'error' | None.
        rate_limited: True when the gateway rejected the request pre-upstream.
    """
    provider: Optional[str] = None
    channel_id: Optional[str] = None
    channel_type: Optional[str] = None
    requested_model: Optional[str] = None
    resolved_model: Optional[str] = None
    route_reason: Optional[str] = None
    policy_name: Optional[str] = None
    fallback_from_channel_id: Optional[str] = None
    retryable: bool = False
    cache_status: Optional[str] = None
    rate_limited: bool = False


@dataclass
class AttemptContext:
    """Context for one real upstream request (spec §8.3).

    Attributes:
        attempt_index: 1-based index of this attempt under the Router.
        provider: Upstream provider name.
        channel_id: Internal channel ID used for this attempt.
        channel_type: Channel type.
        resolved_model: Model actually requested upstream.
        upstream_base_url_hash: Hash of the upstream base URL (privacy).
        timeout_ms: Timeout configured for this attempt.
    """
    attempt_index: int = 1
    provider: Optional[str] = None
    channel_id: Optional[str] = None
    channel_type: Optional[str] = None
    resolved_model: Optional[str] = None
    upstream_base_url_hash: Optional[str] = None
    timeout_ms: Optional[int] = None


@dataclass
class AttemptResult:
    """Outcome of one real upstream request (internal aggregation unit).

    Attributes:
        attempt_index: 1-based attempt index this result belongs to.
        channel_id: Internal channel used for this attempt.
        http_status_code: Upstream HTTP status (None on transport error).
        duration_ms: Upstream request duration in milliseconds.
        ttft_ms: Time to first token for streaming attempts (optional).
        error: Classified failure (None on success).
        finish_reason: Upstream finish reason (optional).
        usage: Provider-returned usage (may be present on failed attempts).
        cost: Computed cost for this attempt.
        success: True when the attempt completed without a classified error.
    """
    attempt_index: int = 1
    channel_id: Optional[str] = None
    http_status_code: Optional[int] = None
    duration_ms: Optional[float] = None
    ttft_ms: Optional[float] = None
    error: Optional[GatewayError] = None
    finish_reason: Optional[str] = None
    usage: Optional[NormalizedUsage] = None
    cost: Optional[NormalizedCost] = None
    success: bool = True


_INTERNAL_STATE = {
    "request": None, "route_decision": None,
}

# Frozen trace origins (spec: trace origin three-value semantics).
ORIGIN_SDK_CONTEXT = "sdk_context"
ORIGIN_REMOTE_TRACEPARENT = "remote_traceparent"
ORIGIN_GATEWAY_ROOT = "gateway_root"

_ALL_ZERO_TRACE_ID = "0" * 32


@dataclass(frozen=True)
class ResolvedGatewayParent:
    """Explicit result of Router parent resolution (never inferred indirectly).

    Attributes:
        trace_id: Always a valid W3C TraceID — 32 lowercase hex, never all zero.
        parent_span_id: Parent span ID for sdk/remote origins; None for a root.
        origin: One of ``sdk_context`` / ``remote_traceparent`` / ``gateway_root``.
        upstream_trace_present: True iff origin is sdk_context or remote_traceparent.
    """
    trace_id: str
    parent_span_id: Optional[str]
    origin: str
    upstream_trace_present: bool

    @property
    def trace_origin_attribute(self) -> str:
        """The frozen ``gateway.trace_origin`` value for this origin."""
        return {
            ORIGIN_SDK_CONTEXT: "sdk",
            ORIGIN_REMOTE_TRACEPARENT: "remote",
            ORIGIN_GATEWAY_ROOT: "gateway",
        }.get(self.origin, "gateway")


def _new_valid_trace_id() -> str:
    """Generate a valid W3C TraceID: 32 lowercase hex, never all zeros."""
    trace_id = generate_trace_id()
    if not trace_id or len(trace_id) != 32 or trace_id == _ALL_ZERO_TRACE_ID:
        # Regenerate once; uuid4 hex is never all-zero in practice, but the
        # contract forbids ever reporting one.
        trace_id = generate_trace_id()
        if not trace_id or len(trace_id) != 32 or trace_id == _ALL_ZERO_TRACE_ID:
            trace_id = "1" + (trace_id or "0" * 32)[1:]
    return trace_id.lower()


def _is_valid_trace_id(trace_id: Optional[str]) -> bool:
    """True iff trace_id is 32 hex chars and not all zeros."""
    if not trace_id or not isinstance(trace_id, str):
        return False
    tid = trace_id.lower()
    if len(tid) != 32 or tid == _ALL_ZERO_TRACE_ID:
        return False
    return all(c in "0123456789abcdef" for c in tid)


class RouterSpan:
    """Context manager for a Router GATEWAY span (spec §4, §9.2).

    On enter, creates a GATEWAY span with ``gateway.span_role = router`` whose
    parent is the SDK LLM span when one is active, else the Root. On exit the
    span ends with the final channel/status/error and the aggregate Usage/Cost.

    Args:
        tracer: SDK Tracer (used for sampling + reporter).
        request_context: GatewayRequestContext from the adapter.
        route_decision: RouteDecision from the adapter.
        sampled: Whether this trace reports to the backend.
        privacy: Optional PrivacyGuard for channel-ID hashing.
        router_registry / attempt_registry: Optional registries for cleanup.
    """

    def __init__(
        self,
        tracer=None,
        request_context=None,
        route_decision: Optional[RouteDecision] = None,
        sampled: bool = True,
        privacy: Optional[PrivacyGuard] = None,
        router_registry: Optional[RouterRegistry] = None,
        attempt_registry: Optional[AttemptRegistry] = None,
        upstream_trace_id: Optional[str] = None,
        upstream_parent_span_id: Optional[str] = None,
    ):
        self._tracer = tracer
        self._request_context = request_context
        self._route_decision = route_decision
        self._sampled = sampled
        self._privacy = privacy or PrivacyGuard()
        self._router_registry = router_registry
        self._attempt_registry = attempt_registry
        if self._attempt_registry is None:
            self._attempt_registry = AttemptRegistry()
        self._upstream_trace_id = upstream_trace_id
        self._upstream_parent_span_id = upstream_parent_span_id

        self._span: Optional[Span] = None
        self._token = None
        self._ctx_token = None
        self._closed = False
        self._started_at: float = 0.0
        self._ended_at: float = 0.0
        self._resolved_parent: Optional[ResolvedGatewayParent] = None

        self._attempts: list[AttemptSpan] = []
        self._open_attempts: dict[str, AttemptSpan] = {}
        self._index_lock = threading.Lock()
        self._used_attempt_indices: set[int] = set()
        self._attempt_count = 0
        self._success_count = 0
        self._fail_count = 0
        self._retry_count = 0
        self._fallback_count = 0
        self._final_channel_id: Optional[str] = None
        self._final_http_status: Optional[int] = None
        self._final_error: Optional[GatewayError] = None
        self._cache_status: Optional[str] = None
        self._ttft_ms: Optional[float] = None
        self._queue_duration_ms: Optional[float] = None
        self._auth_duration_ms: Optional[float] = None
        self._route_duration_ms: Optional[float] = None

        self._usage_aggregate: Optional[NormalizedUsage] = None
        self._cost_aggregate: Optional[NormalizedCost] = None

        self.recorder = GatewayEventRecorder(span=None, privacy=self._privacy)

    # ── lifecycle ──

    def start(self) -> "RouterSpan":
        """Create and start the Router span (fail-open)."""
        try:
            resolved = self._resolve_parent()
            self._resolved_parent = resolved
            trace_id = resolved.trace_id
            parent_span_id = resolved.parent_span_id
            span_id = generate_span_id()
            span = Span(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                span_name="gateway.router",
                span_kind=SpanKind.GATEWAY,
            )
            rc = self._request_context
            if rc is not None:
                span.set_attribute(ATTR_GATEWAY["name"], rc.gateway_name)
                if rc.gateway_version:
                    span.set_attribute(ATTR_GATEWAY["version"], rc.gateway_version)
                if rc.request_id:
                    span.set_attribute(ATTR_GATEWAY["request_id"], rc.request_id)
                span.set_attribute(ATTR_GATEWAY["protocol"], rc.protocol or "openai-compatible")
                if rc.route:
                    span.set_attribute(ATTR_GATEWAY["route"], rc.route)
                if rc.requested_model:
                    span.set_attribute(ATTR_ROUTER["requested_model"], rc.requested_model)
                # Full association field set — written to the Span top-level
                # fields using the EXISTING Span Record naming (``business_scene``,
                # never a second ``business_scenario`` spelling), sanitized.
                if rc.user_id:
                    span.user_id = self._privacy.sanitize_string(rc.user_id)
                if rc.session_id:
                    span.session_id = self._privacy.sanitize_string(rc.session_id)
                if rc.message_id:
                    span.message_id = self._privacy.sanitize_string(rc.message_id)
                if rc.app_name:
                    span.app_name = self._privacy.sanitize_string(rc.app_name)
                if rc.business_scenario:
                    span.business_scene = self._privacy.sanitize_string(rc.business_scenario)
            # Frozen trace-origin semantics, derived from the explicit
            # resolution — never inferred from "whether a parent exists".
            span.set_attribute(ATTR_GATEWAY["trace_origin"], resolved.trace_origin_attribute)
            span.set_attribute(ATTR_GATEWAY["upstream_trace_present"], resolved.upstream_trace_present)
            span.set_attribute(ATTR_GATEWAY["span_role"], ROUTER)

            rd = self._route_decision
            if rd is not None:
                if rd.provider:
                    span.set_attribute(ATTR_ROUTER["provider"], rd.provider)
                if rd.resolved_model:
                    span.set_attribute(ATTR_ROUTER["resolved_model"], rd.resolved_model)
                if rd.channel_type:
                    span.set_attribute(ATTR_ROUTER["channel_type"], rd.channel_type)
                if rd.route_reason:
                    span.set_attribute(ATTR_ROUTER["route_reason"], rd.route_reason)
                if rd.policy_name:
                    span.set_attribute(ATTR_ROUTER["policy_name"], rd.policy_name)
                if rd.channel_id:
                    self._final_channel_id = rd.channel_id
                    span.set_attribute(
                        ATTR_ROUTER["channel_id"],
                        self._privacy.hash_channel_id(rd.channel_id),
                    )
                if rd.cache_status:
                    self._cache_status = rd.cache_status
                    span.set_attribute(ATTR_ROUTER["cache_status"], rd.cache_status)

            span.start()
            self._span = span
            self._started_at = span.start_time

            # Bind recorder to the span + register + set context.
            self.recorder.bind(span)
            if self._router_registry is not None:
                self._router_registry.register(trace_id, span_id, self)
            self._token, _ = GatewayContext.enter_router(self)
            self._ctx_token = self._token

            # Route lifecycle events (gateway.route.started → route.selected).
            if rd is not None:
                self.recorder.record("gateway.route.started", {})
                self.recorder.route_selected(
                    channel_id=rd.channel_id,
                    provider=rd.provider,
                    resolved_model=rd.resolved_model,
                    reason=rd.route_reason,
                )
        except Exception as e:
            logger.error("Router span start failed: %s", e)
        return self

    def _resolve_parent(self) -> ResolvedGatewayParent:
        """Resolve the Router's parent into an explicit, frozen result.

        Order: in-process SDK context → upstream ``traceparent`` → local root.
        A local root always gets a freshly generated, valid (non-zero, 32-hex)
        TraceID — a Router NEVER reports a null or all-zero TraceID.
        """
        try:
            from ..context import get_current_context
            ctx = get_current_context()
            if ctx is not None:
                trace_id = getattr(ctx, "trace_id", None)
                if _is_valid_trace_id(trace_id):
                    return ResolvedGatewayParent(
                        trace_id=trace_id.lower(),
                        parent_span_id=getattr(ctx, "span_id", None),
                        origin=ORIGIN_SDK_CONTEXT,
                        upstream_trace_present=True,
                    )
        except Exception:
            pass
        if _is_valid_trace_id(self._upstream_trace_id):
            return ResolvedGatewayParent(
                trace_id=self._upstream_trace_id.lower(),
                parent_span_id=self._upstream_parent_span_id,
                origin=ORIGIN_REMOTE_TRACEPARENT,
                upstream_trace_present=True,
            )
        return ResolvedGatewayParent(
            trace_id=_new_valid_trace_id(),
            parent_span_id=None,
            origin=ORIGIN_GATEWAY_ROOT,
            upstream_trace_present=False,
        )

    def close(self):
        """End the Router span at a terminal state (fail-open).

        Any still-open Attempt is force-closed first (never just dropped from
        the registry). Final status reflects the last attempt or a route-level
        decision (cache hit / rate-limit rejection). Usage/Cost aggregate and
        all counters are recorded before end. Registry + ContextVar always
        cleaned.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._force_close_open_attempts()
            try:
                if self._span is not None:
                    self._record_terminal_event()
                    self._span.end()
                    self._ended_at = self._span.end_time
                    self._apply_aggregates()
                    self._apply_final_status()
                    if self._sampled and self._tracer is not None:
                        try:
                            self._tracer.reporter.report(self._span.to_record())
                        except Exception as e:
                            logger.error("Failed to report Router span: %s", e)
            except Exception as e:
                logger.error("Router span end failed: %s", e)
        finally:
            self._cleanup()

    def _force_close_open_attempts(self):
        """Force-close every Attempt still open when the Router finalizes.

        A business exception between Attempt start and close must not leak an
        open span / registry entry / active-attempt ContextVar.
        """
        try:
            open_attempts = list(self._open_attempts.values())
        except Exception:
            open_attempts = []
        for attempt in open_attempts:
            try:
                attempt.force_close(
                    category=ErrorCategory.GATEWAY_INTERNAL,
                    reason="router_finalized_with_open_attempt",
                )
            except Exception as e:
                logger.error("Router force-close of open attempt failed: %s", e)
        try:
            self._open_attempts.clear()
        except Exception:
            pass

    # ── open-attempt registry ──

    def register_open_attempt(self, attempt: AttemptSpan):
        """Track an Attempt as open (called on Attempt start)."""
        try:
            key = attempt.span.span_id if attempt.span is not None else str(id(attempt))
            self._open_attempts[key] = attempt
        except Exception as e:
            logger.error("Router open-attempt register failed: %s", e)

    def unregister_open_attempt(self, attempt: AttemptSpan):
        """Stop tracking an Attempt (called on Attempt close/force_close)."""
        try:
            key = attempt.span.span_id if attempt.span is not None else str(id(attempt))
            self._open_attempts.pop(key, None)
        except Exception as e:
            logger.error("Router open-attempt unregister failed: %s", e)

    @property
    def open_attempt_count(self) -> int:
        try:
            return len(self._open_attempts)
        except Exception:
            return 0

    def _record_terminal_event(self):
        """Record ``gateway.response.completed``/``gateway.response.failed``
        exactly once, before the span ends (P1-3 lifecycle wiring)."""
        try:
            if self._span is None:
                return
            if self._final_error is not None:
                self.recorder.response_failed(
                    error_category=self._final_error.category,
                    http_status_code=self._final_http_status,
                )
            else:
                self.recorder.response_completed(http_status_code=self._final_http_status)
        except Exception as e:
            logger.error("Router terminal event failed: %s", e)

    def _apply_aggregates(self):
        try:
            if self._span is None:
                return
            span = self._span
            span.set_attribute(ATTR_ROUTER["attempt_count"], self._attempt_count)
            span.set_attribute(ATTR_ROUTER["retry_count"], self._retry_count)
            span.set_attribute(ATTR_ROUTER["fallback_count"], self._fallback_count)
            if self._cache_status:
                span.set_attribute(ATTR_ROUTER["cache_status"], self._cache_status)
            if self._final_channel_id:
                span.set_attribute(
                    ATTR_ROUTER["channel_id"],
                    self._privacy.hash_channel_id(self._final_channel_id),
                )
            if self._ttft_ms is not None:
                span.set_attribute(ATTR_ROUTER["ttft_ms"], self._ttft_ms)
            if self._queue_duration_ms is not None:
                span.set_attribute(ATTR_ROUTER["queue_duration_ms"], self._queue_duration_ms)
            if self._auth_duration_ms is not None:
                span.set_attribute(ATTR_ROUTER["auth_duration_ms"], self._auth_duration_ms)
            if self._route_duration_ms is not None:
                span.set_attribute(ATTR_ROUTER["route_duration_ms"], self._route_duration_ms)
            if self._ended_at > 0 and self._started_at > 0:
                span.set_attribute(
                    ATTR_ROUTER["total_duration_ms"],
                    round((self._ended_at - self._started_at) * 1000, 2),
                )
            if self._final_http_status is not None:
                span.set_attribute(ATTR_ROUTER["final_http_status_code"], self._final_http_status)
            if self._final_error is not None:
                span.set_attribute(ATTR_ROUTER["final_error_category"], self._final_error.category)
                if self._final_error.type:
                    span.set_attribute(ATTR_ROUTER["final_error_type"], self._final_error.type)
            for key, value in usage_to_attributes(self._usage_aggregate).items():
                span.set_attribute(key, value)
            for key, value in cost_to_attributes(self._cost_aggregate).items():
                span.set_attribute(key, value)
        except Exception as e:
            logger.error("Router aggregate apply failed: %s", e)

    def _apply_final_status(self):
        try:
            if self._span is None:
                return
            if self._cache_status == "hit":
                self._span.set_status("OK")
                return
            if self._route_decision is not None and self._route_decision.rate_limited:
                self._span.set_status("ERROR")
                return
            if self._final_error is not None:
                # Router and Attempt terminal states must agree: a final
                # classified error (incl. client_cancelled / timeout /
                # stream_interrupted) makes the Router ERROR.
                self._span.set_status("ERROR")
            elif self._fail_count > 0 and self._success_count == 0:
                # Every attempt failed but no classified error survived —
                # still an error, never OK.
                self._span.set_status("ERROR")
            else:
                self._span.set_status("OK")
        except Exception as e:
            logger.error("Router final status failed: %s", e)

    def _cleanup(self):
        """Unregister + clear router ContextVar (fail-open)."""
        try:
            if self._router_registry is not None and self._span is not None:
                self._router_registry.remove(self._span.trace_id, self._span.span_id)
        except Exception as e:
            logger.error("Router registry cleanup failed: %s", e)
        try:
            if self._token is not None:
                GatewayContext.exit_router(self._token)
                self._token = None
        except Exception as e:
            logger.error("Router context cleanup failed: %s", e)
        # Clear the whole gateway context (both slots) only when this Router
        # is the active one — Router terminal state is the ONLY place allowed
        # to clear the Router slot.
        try:
            from .context import clear_gateway_context
            current = GatewayContext.get()
            if current.router is self or current.router is None:
                clear_gateway_context()
        except Exception:
            pass

    def __enter__(self) -> "RouterSpan":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and self._final_error is None and self._attempt_count == 0:
            from .errors import classify_error
            self._final_error = classify_error(exc_val)
        self.close()
        return False  # do not suppress business exceptions

    # ── Attempt lifecycle ──

    def allocate_attempt_index(self, explicit_index: Optional[int] = None) -> int:
        """Allocate the attempt index for a new Attempt (thread-safe).

        Rules (frozen):
        - No explicit value → next auto-allocated index.
        - Explicit valid positive integer, unused → use it.
        - Explicit duplicate → remap to the next available value + warning.
        - Zero / negative / non-integer → fall back to auto-allocation.
        """
        with self._index_lock:
            candidate: Optional[int] = None
            if explicit_index is not None:
                try:
                    if isinstance(explicit_index, bool):
                        raise ValueError("bool is not a valid attempt index")
                    parsed = int(explicit_index)
                    if parsed >= 1 and parsed == explicit_index:
                        candidate = parsed
                except (TypeError, ValueError):
                    candidate = None
            if candidate is not None:
                if candidate not in self._used_attempt_indices:
                    self._used_attempt_indices.add(candidate)
                    self._attempt_count = len(self._used_attempt_indices)
                    return candidate
                logger.warning(
                    "Duplicate explicit attempt_index %s — remapping to next available",
                    candidate,
                )
            # Auto-allocate the smallest unused positive index.
            next_index = 1
            while next_index in self._used_attempt_indices:
                next_index += 1
            self._used_attempt_indices.add(next_index)
            self._attempt_count = len(self._used_attempt_indices)
            return next_index

    def attempt(
        self,
        attempt_index: Optional[int] = None,
        provider: Optional[str] = None,
        channel_id: Optional[str] = None,
        channel_type: Optional[str] = None,
        resolved_model: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> AttemptSpan:
        """Create a fresh AttemptSpan under this Router (spec §13.2).

        Each call produces a new, unique Attempt span — never reused. The
        index is allocated by the Router (default increments; duplicates are
        remapped with a warning; invalid values fall back to auto-allocation).
        """
        index = self.allocate_attempt_index(attempt_index)
        attempt = AttemptSpan(
            router=self,
            attempt_index=index,
            provider=provider,
            channel_id=channel_id,
            channel_type=channel_type,
            resolved_model=resolved_model,
            timeout_ms=timeout_ms,
            privacy=self._privacy,
            registry=self._attempt_registry,
            tracer=self._tracer,
            sampled=self._sampled,
        )
        self._attempts.append(attempt)
        return attempt

    def register_attempt_result(self, result: AttemptResult):
        """Aggregate one attempt's outcome into the Router (fail-open).

        Failed attempts with Provider-returned usage still contribute to the
        aggregate (spec §12.2).
        """
        try:
            if result.success:
                self._success_count += 1
                if result.channel_id:
                    self._final_channel_id = result.channel_id
                if result.http_status_code is not None:
                    self._final_http_status = result.http_status_code
            else:
                self._fail_count += 1
                if result.http_status_code is not None:
                    self._final_http_status = result.http_status_code
                if result.channel_id:
                    self._final_channel_id = result.channel_id
            # Final error reflects the LAST attempt's outcome: a later success
            # clears the error left by an earlier failed attempt (spec §13.2).
            self._final_error = result.error
            if result.ttft_ms is not None and self._ttft_ms is None:
                self._ttft_ms = result.ttft_ms
            # Always aggregate usage/cost (including failed attempts).
            if result.usage is not None:
                self._usage_aggregate = add_usage(self._usage_aggregate, result.usage)
            if result.cost is not None:
                self._cost_aggregate = add_cost(self._cost_aggregate, result.cost)
        except Exception as e:
            logger.error("Router attempt aggregation failed: %s", e)

    def set_usage_aggregate(self, usage: NormalizedUsage):
        """Directly set the Router usage aggregate (e.g. cache hit with usage)."""
        self._usage_aggregate = usage

    def set_cost_aggregate(self, cost: NormalizedCost):
        """Directly set the Router cost aggregate."""
        self._cost_aggregate = cost

    # ── route decision helpers (spec §13-14) ──

    def retry_scheduled(self, attempt_index: Optional[int] = None, delay_ms: Optional[float] = None,
                        reason: Optional[str] = None) -> bool:
        """Record a ``gateway.retry.scheduled`` event and bump retry_count."""
        self._retry_count += 1
        return self.recorder.retry_scheduled(attempt_index=attempt_index, delay_ms=delay_ms, reason=reason)

    def fallback_selected(self, from_channel_id: Optional[str] = None, to_channel_id: Optional[str] = None,
                          reason: Optional[str] = None) -> bool:
        """Record a single ``gateway.fallback.selected`` event (from → to).

        The from-channel must differ from the to-channel; a fallback without a
        channel switch is forbidden (spec §10/§13.3).
        """
        if from_channel_id is not None and to_channel_id is not None and from_channel_id == to_channel_id:
            logger.warning(
                "Fallback ignored: from_channel_id == to_channel_id (%s)", to_channel_id
            )
            return False
        self._fallback_count += 1
        if to_channel_id is not None:
            self._final_channel_id = to_channel_id
        return self.recorder.fallback_selected(
            from_channel_id=from_channel_id, to_channel_id=to_channel_id, reason=reason
        )

    def set_cache_status(self, status: str):
        """Record a cache decision: hit → no attempt, cache_status=hit."""
        self._cache_status = status
        try:
            if self._span is not None:
                self._span.set_attribute(ATTR_ROUTER["cache_status"], status)
        except Exception as e:
            logger.error("Router cache status failed: %s", e)
        if status == "hit":
            self.recorder.cache_hit()
        elif status == "miss":
            self.recorder.cache_miss()
        elif status == "bypass":
            self.recorder.cache_bypass()

    def set_rate_limited(self, category: str = ErrorCategory.RATE_LIMIT):
        """Record a rate-limit rejection: Router ERROR, attempt_count=0."""
        self._final_error = GatewayError(category=category, type="RateLimitError",
                                         message="gateway rate limit rejected", retryable=True)
        try:
            if self._span is not None:
                self._span.set_status("ERROR")
                self._span.set_attribute(ATTR_ROUTER["final_error_category"], category)
        except Exception as e:
            logger.error("Router rate-limit set failed: %s", e)
        self.recorder.rate_limit_rejected()

    def set_ttft(self, ttft_ms: Optional[float]):
        """Record the gateway TTFT (streaming; exactly once)."""
        if self._ttft_ms is None and ttft_ms is not None:
            self._ttft_ms = ttft_ms
            try:
                if self._span is not None:
                    self._span.set_attribute(ATTR_ROUTER["ttft_ms"], ttft_ms)
            except Exception as e:
                logger.error("Router ttft set failed: %s", e)

    def set_duration_breakdown(self, queue_duration_ms: Optional[float] = None,
                               auth_duration_ms: Optional[float] = None,
                               route_duration_ms: Optional[float] = None):
        """Record router stage durations (fail-open)."""
        self._queue_duration_ms = queue_duration_ms
        self._auth_duration_ms = auth_duration_ms
        self._route_duration_ms = route_duration_ms

    # ── properties ──

    @property
    def resolved_parent(self) -> Optional[ResolvedGatewayParent]:
        """The explicit parent resolution (available after ``start()``)."""
        return self._resolved_parent

    @property
    def open_attempts(self) -> list:
        """Snapshot of currently open Attempts."""
        try:
            return list(self._open_attempts.values())
        except Exception:
            return []

    @property
    def span(self) -> Optional[Span]:
        return self._span

    @property
    def attempts(self) -> list:
        return list(self._attempts)

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    @property
    def success_count(self) -> int:
        return self._success_count

    @property
    def fail_count(self) -> int:
        return self._fail_count

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def fallback_count(self) -> int:
        return self._fallback_count

    @property
    def final_channel_id(self) -> Optional[str]:
        return self._final_channel_id

    @property
    def final_error(self) -> Optional[GatewayError]:
        return self._final_error

    @property
    def cache_status(self) -> Optional[str]:
        return self._cache_status

    @property
    def usage_aggregate(self) -> Optional[NormalizedUsage]:
        return self._usage_aggregate

    @property
    def cost_aggregate(self) -> Optional[NormalizedCost]:
        return self._cost_aggregate

    @property
    def ttft_ms(self) -> Optional[float]:
        return self._ttft_ms
