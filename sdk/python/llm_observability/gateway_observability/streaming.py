"""Streaming lifecycle wrapper (spec §15, runtime spec).

Keeps Router/Attempt spans open until a real terminal state:
  - normal full consumption
  - upstream ``[DONE]``
  - client disconnect / cancellation
  - generator ``close()`` / async generator ``aclose()`` / ``GeneratorExit``
  - upstream timeout / connection error

Spans NEVER end — and no success ``AttemptResult`` is ever aggregated to the
Router — at response-header receipt, connection establishment, first-token
arrival, or the return of a ``StreamingResponse`` object.

Every terminal path funnels through one idempotent finalizer that, exactly
once: records the terminal event → closes the Attempt → aggregates the final
``AttemptResult`` into the Router (statuses always consistent) → closes the
Router.

TTFT is measured from the real upstream request start (the Attempt span's
start time) to the first *meaningful model content*; SSE keepalives, empty
strings, metadata-only chunks, usage-only chunks, and ``[DONE]`` never
trigger TTFT.
"""
import asyncio
import logging
import time
from typing import Any, Optional

from .errors import ErrorCategory, GatewayError
from .router_span import RouterSpan, AttemptResult
from .usage import add_usage

logger = logging.getLogger("llm_obs.gateway.streaming")


def is_meaningful_content(chunk: Any) -> bool:
    """True only for real model content (never keepalive / metadata / usage)."""
    if chunk is None:
        return False
    if isinstance(chunk, str):
        text = chunk.strip()
        if not text or text == "[DONE]":
            return False
        # Raw SSE frame text (keepalive comments / event envelopes) is not
        # model content.
        if text.startswith(":") or text.startswith("data:") or text.startswith("event:"):
            return False
        return True
    if isinstance(chunk, dict):
        # Usage-only chunk (e.g. the terminal usage frame).
        if "usage" in chunk and not chunk.get("choices") and not chunk.get("delta"):
            return False
        # OpenAI-style chunk: meaningful when any choice carries content,
        # reasoning content, or tool calls.
        choices = chunk.get("choices")
        if choices:
            try:
                for choice in choices:
                    delta = choice.get("delta") or choice.get("message") or {}
                    if not isinstance(delta, dict):
                        continue
                    if delta.get("content"):
                        return True
                    if delta.get("reasoning_content"):
                        return True
                    if delta.get("tool_calls"):
                        return True
            except Exception:
                return False
            return False
        # Anthropic-style events.
        event_type = chunk.get("type")
        if event_type in ("content_block_delta", "content_block_start"):
            return True
        # message_start / ping / message_delta (usage-only) etc. are not.
        return False
    # Objects (SDK chunk types): meaningful when they expose content.
    try:
        choices = getattr(chunk, "choices", None)
        if choices:
            for choice in choices:
                delta = getattr(choice, "delta", None) or getattr(choice, "message", None)
                if delta is None:
                    continue
                if getattr(delta, "content", None):
                    return True
                if getattr(delta, "reasoning_content", None):
                    return True
                if getattr(delta, "tool_calls", None):
                    return True
            return False
        if getattr(chunk, "usage", None) is not None:
            return False
    except Exception:
        return False
    return False


def extract_chunk_usage(chunk: Any):
    """Best-effort raw usage payload from a chunk (dict or object)."""
    try:
        if isinstance(chunk, dict):
            return chunk.get("usage")
        return getattr(chunk, "usage", None)
    except Exception:
        return None


class _TerminalFinalizer:
    """Shared idempotent terminal funnel for sync/async streams.

    Guarantees: terminal event → attempt close → exactly-once Router
    aggregation with a consistent final state → Router close.
    """

    def __init__(self, router: Optional[RouterSpan], attempt, usage_normalizer=None,
                 upstream_status: Optional[int] = None, duration_ms: Optional[float] = None,
                 connect_duration_ms: Optional[float] = None,
                 cost_calculator=None, resolved_model: Optional[str] = None):
        self._router = router
        self._attempt = attempt
        self._usage_normalizer = usage_normalizer
        self._cost_calculator = cost_calculator
        self._resolved_model = resolved_model
        self._upstream_status = upstream_status
        self._duration_ms = duration_ms
        self._connect_duration_ms = connect_duration_ms
        self._finalized = False
        self._stream_usage = None  # usage captured from terminal chunks
        self._stream_cost = None   # cost computed from the terminal usage

    @property
    def finalized(self) -> bool:
        return self._finalized

    def capture_usage(self, raw_usage: Any):
        """Capture usage from a stream chunk (terminal or partial)."""
        if raw_usage is None:
            return
        try:
            if self._usage_normalizer is not None:
                normalized = self._usage_normalizer.normalize(raw_usage)
                if normalized is not None:
                    self._stream_usage = normalized
        except Exception as e:
            logger.error("Gateway stream usage capture failed: %s", e)

    def _compute_cost(self, usage):
        """Compute the streaming attempt cost from captured usage (fail-open).

        Mirrors the non-streaming ``runtime.finalize_attempt`` cost path: the
        resolved model's pricing table decides priced vs unpriced; a calc
        failure leaves cost unset and never propagates.
        """
        if usage is None or self._cost_calculator is None:
            return None
        try:
            return self._cost_calculator.calculate(usage, model=self._resolved_model)
        except Exception as e:
            logger.error("Gateway stream cost computation failed: %s", e)
            return None

    def _apply_upstream_facts(self):
        """Write header-time upstream facts onto the Attempt (no finalize).

        Only non-None values are applied — never clobber already-recorded
        status/duration facts.
        """
        if self._attempt is None:
            return
        if self._upstream_status is None and self._duration_ms is None and self._connect_duration_ms is None:
            return
        try:
            self._attempt.set_upstream_status(
                status_code=self._upstream_status,
                duration_ms=self._duration_ms,
                connect_duration_ms=self._connect_duration_ms,
            )
        except Exception as e:
            logger.error("Gateway stream upstream facts failed: %s", e)

    # ── terminal paths ──

    def finalize_success(self):
        if self._finalized:
            return
        self._finalized = True
        try:
            if self._router is not None:
                self._router.recorder.stream_completed()
            usage = self._apply_usage_to_attempt()
            self._close_attempt()
            status = self._upstream_status
            if status is None:
                status = getattr(self._attempt, "status", None) if self._attempt else None
            self._aggregate_to_router(AttemptResult(
                attempt_index=getattr(self._attempt, "attempt_index", 1) if self._attempt else 1,
                channel_id=getattr(self._attempt, "channel_id", None) if self._attempt else None,
                http_status_code=status,
                duration_ms=self._duration_ms,
                usage=usage,
                cost=self._stream_cost,
                success=True,
            ))
        except Exception as e:
            logger.error("Gateway stream success finalize failed: %s", e)
        finally:
            self._close_router()

    def finalize_error(self, error: BaseException):
        if self._finalized:
            return
        self._finalized = True
        try:
            gateway_error = self._classify(error)
            if self._attempt is not None:
                try:
                    self._attempt.set_error(gateway_error)
                except Exception:
                    pass
            if self._router is not None:
                self._router.recorder.response_failed(error_category=gateway_error.category)
            usage = self._apply_usage_to_attempt()
            self._close_attempt()
            # Router final_error_category == Attempt error_category (consistent).
            self._aggregate_to_router(AttemptResult(
                attempt_index=getattr(self._attempt, "attempt_index", 1) if self._attempt else 1,
                channel_id=getattr(self._attempt, "channel_id", None) if self._attempt else None,
                http_status_code=self._upstream_status,
                duration_ms=self._duration_ms,
                error=gateway_error,
                usage=usage,
                cost=self._stream_cost,
                success=False,
            ))
        except Exception as e:
            logger.error("Gateway stream error finalize failed: %s", e)
        finally:
            self._close_router()

    def finalize_cancelled(self):
        if self._finalized:
            return
        self._finalized = True
        try:
            gateway_error = GatewayError(
                category=ErrorCategory.CLIENT_CANCELLED,
                type="ClientDisconnected",
                message="stream cancelled by client",
                retryable=False,
            )
            if self._attempt is not None:
                try:
                    self._attempt.set_error(gateway_error)
                except Exception:
                    pass
            if self._router is not None:
                self._router.recorder.stream_cancelled(
                    error_category=ErrorCategory.CLIENT_CANCELLED
                )
            # Partial usage already returned upstream is still recorded.
            usage = self._apply_usage_to_attempt()
            self._close_attempt()
            self._aggregate_to_router(AttemptResult(
                attempt_index=getattr(self._attempt, "attempt_index", 1) if self._attempt else 1,
                channel_id=getattr(self._attempt, "channel_id", None) if self._attempt else None,
                http_status_code=self._upstream_status,
                duration_ms=self._duration_ms,
                error=gateway_error,
                usage=usage,
                cost=self._stream_cost,
                success=False,
            ))
        except Exception as e:
            logger.error("Gateway stream cancel finalize failed: %s", e)
        finally:
            self._close_router()

    # ── internals ──

    def _classify(self, error: BaseException) -> GatewayError:
        if isinstance(error, GatewayError):
            return error
        try:
            from .errors import classify_error
            classified = classify_error(error)
            # Streaming-only remap (spec §5): an unclassifiable mid-stream
            # interruption is stream_interrupted, never unknown. Global
            # classify_error() behavior is unchanged.
            if classified.category == ErrorCategory.UNKNOWN:
                return GatewayError(
                    category=ErrorCategory.STREAM_INTERRUPTED,
                    type=classified.type or type(error).__name__,
                    message=classified.message,
                    retryable=True,
                )
            return classified
        except Exception:
            return GatewayError(
                category=ErrorCategory.STREAM_INTERRUPTED,
                type=type(error).__name__,
                message="stream interrupted",
                retryable=False,
            )

    def _apply_usage_to_attempt(self):
        """Write captured stream usage + cost onto the Attempt; return usage.

        Cost is computed from the captured terminal Usage via the
        ``CostCalculator`` and the Attempt's ``resolved_model`` (fail-open),
        mirroring the non-streaming cost path so streaming and non-streaming
        attempts carry ``cost.*`` identically.
        """
        usage = self._stream_usage
        if usage is not None and self._attempt is not None:
            try:
                self._attempt.set_usage(usage)
            except Exception as e:
                logger.error("Gateway stream attempt usage failed: %s", e)
        cost = self._compute_cost(usage)
        if cost is not None and self._attempt is not None:
            try:
                self._attempt.set_cost(cost)
            except Exception as e:
                logger.error("Gateway stream attempt cost failed: %s", e)
        self._stream_cost = cost
        return usage

    def _close_attempt(self):
        if self._attempt is None:
            return
        try:
            self._attempt.close()
        except Exception as e:
            logger.error("Gateway stream attempt close failed: %s", e)

    def _aggregate_to_router(self, result: AttemptResult):
        """Aggregate the terminal AttemptResult into the Router exactly once.

        An attempt that was already aggregated via ``runtime.finalize_attempt``
        (non-streaming) is never re-aggregated by a wrapper.
        """
        if self._router is None:
            return
        try:
            if getattr(self._attempt, "_aggregated_to_router", False):
                return
            if self._attempt is not None:
                self._attempt._aggregated_to_router = True
            self._router.register_attempt_result(result)
        except Exception as e:
            logger.error("Gateway stream router aggregation failed: %s", e)

    def _close_router(self):
        if self._router is None:
            return
        try:
            self._router.close()
        except Exception as e:
            logger.error("Gateway stream router close failed: %s", e)


class GatewayStream:
    """Sync streaming wrapper over an iterable of chunks.

    Wraps an upstream chunk iterator (already created under an AttemptSpan).
    The Attempt is finalized on the first terminal state; the Router is
    finalized when the stream ends (the router owns the attempt).
    """

    def __init__(
        self,
        iterable,
        router: RouterSpan,
        attempt,
        runtime_handle=None,
        check_done: bool = True,
        upstream_status: Optional[int] = None,
        duration_ms: Optional[float] = None,
        connect_duration_ms: Optional[float] = None,
    ):
        """Args:
            iterable: Upstream chunk iterator (SSE lines, etc.).
            router: The RouterSpan this stream belongs to.
            attempt: The AttemptSpan for the underlying upstream request.
            runtime_handle: Optional GatewayRuntimeHandle for finalization.
            check_done: When True, detect an upstream ``[DONE]`` marker.
            upstream_status / duration_ms / connect_duration_ms: Header-time
                upstream facts recorded on the Attempt but never finalizing it.
        """
        self._iterable = iterable
        self._router = router
        self._attempt = attempt
        self._handle = runtime_handle
        self._check_done = check_done
        # TTFT baseline: the real upstream request start (Attempt span start),
        # never the wrapper creation time.
        self._request_started = getattr(attempt, "_started_at", 0.0) or time.time()
        self._first_token_time: Optional[float] = None
        self._closed = False
        usage_normalizer = None
        cost_calculator = None
        try:
            if runtime_handle is not None:
                usage_normalizer = runtime_handle._runtime._usage_normalizer
                cost_calculator = runtime_handle._runtime._cost_calculator
        except Exception:
            usage_normalizer = None
        self._finalizer = _TerminalFinalizer(
            router, attempt, usage_normalizer,
            upstream_status=upstream_status, duration_ms=duration_ms,
            connect_duration_ms=connect_duration_ms,
            cost_calculator=cost_calculator,
            resolved_model=getattr(attempt, "_resolved_model", None),
        )
        # Header-time upstream facts (status/duration) recorded now — the
        # attempt is NOT finalized here (rework P0-4: no success at header).
        self._finalizer._apply_upstream_facts()

        # Record stream start (fail-open). No AttemptResult, no aggregation,
        # no span end here.
        try:
            if router is not None:
                router.recorder.stream_started()
        except Exception as e:
            logger.error("Gateway stream start event failed: %s", e)

    @property
    def _finalized(self) -> bool:
        return self._finalizer.finalized

    # ── iteration ──

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        try:
            chunk = next(self._iterable)
            self._observe_chunk(chunk)
            return chunk
        except StopIteration:
            self._finalizer.finalize_success()
            raise
        except Exception as e:
            self._finalizer.finalize_error(e)
            raise

    def _observe_chunk(self, chunk):
        """Per-chunk bookkeeping: usage capture, TTFT, [DONE] detection."""
        self._finalizer.capture_usage(extract_chunk_usage(chunk))
        if self._is_done_marker(chunk):
            self._finalizer.finalize_success()
            return
        if is_meaningful_content(chunk):
            self._record_first_token_if_needed()

    def _is_done_marker(self, chunk) -> bool:
        if not self._check_done:
            return False
        try:
            return chunk == "[DONE]"
        except Exception:
            return False

    def _record_first_token_if_needed(self):
        """Record TTFT exactly once on the first meaningful content chunk."""
        if self._first_token_time is not None:
            return
        self._first_token_time = time.time()
        ttft_ms = round((self._first_token_time - self._request_started) * 1000, 2)
        try:
            if self._router is not None:
                self._router.set_ttft(ttft_ms)
                self._router.recorder.stream_first_token()
            if self._attempt is not None:
                self._attempt.set_upstream_status(ttft_ms=ttft_ms)
        except Exception as e:
            logger.error("Gateway TTFT recording failed: %s", e)

    # ── close ──

    def close(self):
        """Close the stream. Client-cancel semantics (spec §15.3). Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            close_target = getattr(self._iterable, "close", None)
            if callable(close_target):
                close_target()
        except Exception as e:
            logger.error("Gateway stream close failed: %s", e)
        self._finalizer.finalize_cancelled()

    # ── context manager / delegation ──

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and not self._finalized:
            self._finalizer.finalize_cancelled()
        else:
            self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._iterable, name)

    def __del__(self):
        if not self._finalized and not self._closed:
            try:
                self._finalizer.finalize_cancelled()
            except Exception:
                pass


class AsyncGatewayStream:
    """Async streaming wrapper over an async iterator (spec §15).

    Terminal states mirror the sync wrapper plus ``aclose()`` and
    ``GeneratorExit`` / ``asyncio.CancelledError``.
    """

    def __init__(
        self,
        aiterable,
        router: RouterSpan,
        attempt,
        runtime_handle=None,
        check_done: bool = True,
        upstream_status: Optional[int] = None,
        duration_ms: Optional[float] = None,
        connect_duration_ms: Optional[float] = None,
    ):
        self._aiterable = aiterable
        self._router = router
        self._attempt = attempt
        self._handle = runtime_handle
        self._check_done = check_done
        self._request_started = getattr(attempt, "_started_at", 0.0) or time.time()
        self._first_token_time: Optional[float] = None
        self._closed = False
        usage_normalizer = None
        cost_calculator = None
        try:
            if runtime_handle is not None:
                usage_normalizer = runtime_handle._runtime._usage_normalizer
                cost_calculator = runtime_handle._runtime._cost_calculator
        except Exception:
            usage_normalizer = None
        self._finalizer = _TerminalFinalizer(
            router, attempt, usage_normalizer,
            upstream_status=upstream_status, duration_ms=duration_ms,
            connect_duration_ms=connect_duration_ms,
            cost_calculator=cost_calculator,
            resolved_model=getattr(attempt, "_resolved_model", None),
        )
        self._finalizer._apply_upstream_facts()

        try:
            if router is not None:
                router.recorder.stream_started()
        except Exception as e:
            logger.error("Gateway async stream start event failed: %s", e)

    @property
    def _finalized(self) -> bool:
        return self._finalizer.finalized

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        try:
            chunk = await self._aiterable.__anext__()
            self._observe_chunk(chunk)
            return chunk
        except StopAsyncIteration:
            self._finalizer.finalize_success()
            raise
        except asyncio.CancelledError:
            self._finalizer.finalize_cancelled()
            raise
        except GeneratorExit:
            self._finalizer.finalize_cancelled()
            raise
        except Exception as e:
            self._finalizer.finalize_error(e)
            raise

    def _observe_chunk(self, chunk):
        self._finalizer.capture_usage(extract_chunk_usage(chunk))
        if self._is_done_marker(chunk):
            self._finalizer.finalize_success()
            return
        if is_meaningful_content(chunk):
            self._record_first_token_if_needed()

    def _is_done_marker(self, chunk) -> bool:
        if not self._check_done:
            return False
        try:
            return chunk == "[DONE]"
        except Exception:
            return False

    def _record_first_token_if_needed(self):
        if self._first_token_time is not None:
            return
        self._first_token_time = time.time()
        ttft_ms = round((self._first_token_time - self._request_started) * 1000, 2)
        try:
            if self._router is not None:
                self._router.set_ttft(ttft_ms)
                self._router.recorder.stream_first_token()
            if self._attempt is not None:
                self._attempt.set_upstream_status(ttft_ms=ttft_ms)
        except Exception as e:
            logger.error("Gateway async TTFT recording failed: %s", e)

    async def aclose(self):
        """Async close. Client-cancel semantics (spec §15.3). Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            close_target = getattr(self._aiterable, "aclose", None)
            if callable(close_target):
                await close_target()
            elif hasattr(self._aiterable, "close") and callable(self._aiterable.close):
                self._aiterable.close()
        except Exception as e:
            logger.error("Gateway async stream aclose failed: %s", e)
        self._finalizer.finalize_cancelled()

    # ── async context manager ──

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and not self._finalized:
            self._finalizer.finalize_cancelled()
        else:
            await self.aclose()
        return False

    def __getattr__(self, name):
        return getattr(self._aiterable, name)

    def __del__(self):
        if not self._finalized and not self._closed:
            try:
                self._finalizer.finalize_cancelled()
            except Exception:
                pass


def wrap_stream(iterable, router, attempt, runtime_handle=None, check_done: bool = True,
                upstream_status: Optional[int] = None, duration_ms: Optional[float] = None,
                connect_duration_ms: Optional[float] = None) -> GatewayStream:
    """Wrap a sync upstream chunk iterator into a GatewayStream."""
    return GatewayStream(iterable, router, attempt, runtime_handle=runtime_handle,
                         check_done=check_done, upstream_status=upstream_status,
                         duration_ms=duration_ms, connect_duration_ms=connect_duration_ms)


def wrap_async_stream(aiterable, router, attempt, runtime_handle=None, check_done: bool = True,
                      upstream_status: Optional[int] = None, duration_ms: Optional[float] = None,
                      connect_duration_ms: Optional[float] = None) -> AsyncGatewayStream:
    """Wrap an async upstream chunk iterator into an AsyncGatewayStream."""
    return AsyncGatewayStream(aiterable, router, attempt, runtime_handle=runtime_handle,
                              check_done=check_done, upstream_status=upstream_status,
                              duration_ms=duration_ms, connect_duration_ms=connect_duration_ms)
