"""Streaming lifecycle wrapper (spec §15, runtime spec).

Keeps Router/Attempt spans open until a real terminal state:
  - normal full consumption
  - upstream ``[DONE]``
  - client disconnect / cancellation
  - generator ``close()`` / async generator ``aclose()`` / ``GeneratorExit``
  - upstream timeout / connection error

Spans NEVER end at response-header receipt, connection establishment,
first-token arrival, or the return of a ``StreamingResponse`` object.

TTFT is recorded exactly once (``gateway.stream.first_token`` + ``gateway.ttft_ms``
on the Router + ``gateway.upstream_ttft_ms`` on the Attempt). On client cancel
both spans end with ``error_category = client_cancelled`` and every
ContextVar / registry / wrapper reference is cleaned.
"""
import asyncio
import logging
import time
from typing import Any, Optional

from .errors import ErrorCategory, GatewayError
from .router_span import RouterSpan, AttemptResult

logger = logging.getLogger("llm_obs.gateway.streaming")


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
    ):
        """Args:
            iterable: Upstream chunk iterator (SSE lines, etc.).
            router: The RouterSpan this stream belongs to.
            attempt: The AttemptSpan for the underlying upstream request.
            runtime_handle: Optional GatewayRuntimeHandle for finalization.
            check_done: When True, detect an upstream ``[DONE]`` marker.
        """
        self._iterable = iterable
        self._router = router
        self._attempt = attempt
        self._handle = runtime_handle
        self._check_done = check_done
        self._started = time.time()
        self._first_token_time: Optional[float] = None
        self._finalized = False
        self._closed = False

        # Record stream start (fail-open).
        try:
            if router is not None:
                router.recorder.stream_started()
        except Exception as e:
            logger.error("Gateway stream start event failed: %s", e)

    # ── iteration ──

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        try:
            chunk = next(self._iterable)
            self._record_first_token_if_needed()
            self._check_done_marker(chunk)
            return chunk
        except StopIteration:
            self._finalize_success()
            raise
        except Exception as e:
            self._finalize_error(e)
            raise

    def _record_first_token_if_needed(self):
        """Record TTFT exactly once on the first meaningful chunk."""
        if self._first_token_time is not None:
            return
        self._first_token_time = time.time()
        ttft_ms = round((self._first_token_time - self._started) * 1000, 2)
        try:
            if self._router is not None:
                self._router.set_ttft(ttft_ms)
                self._router.recorder.stream_first_token()
            if self._attempt is not None:
                self._attempt.set_upstream_status(ttft_ms=ttft_ms)
        except Exception as e:
            logger.error("Gateway TTFT recording failed: %s", e)

    def _check_done_marker(self, chunk):
        """Detect an upstream ``[DONE]`` SSE terminator."""
        if not self._check_done:
            return
        try:
            if chunk == "[DONE]":
                self._finalize_success()
        except Exception:
            pass

    # ── close ──

    def close(self):
        """Close the stream. Client-cancel semantics (spec §15.3)."""
        if self._closed:
            return
        self._closed = True
        try:
            close_target = getattr(self._iterable, "close", None)
            if callable(close_target):
                close_target()
        except Exception as e:
            logger.error("Gateway stream close failed: %s", e)
        if not self._finalized:
            self._finalize_cancelled()

    def _finalize_success(self):
        if self._finalized:
            return
        self._finalized = True
        try:
            if self._router is not None:
                self._router.recorder.stream_completed()
            self._finalize_attempt(success=True)
        except Exception as e:
            logger.error("Gateway stream success finalize failed: %s", e)
        finally:
            self._finalize_router()

    def _finalize_error(self, error: BaseException):
        if self._finalized:
            return
        self._finalized = True
        try:
            if self._router is not None:
                self._router.recorder.response_failed(
                    error_category=ErrorCategory.STREAM_INTERRUPTED
                )
            self._finalize_attempt(success=False, error=error)
        except Exception as e:
            logger.error("Gateway stream error finalize failed: %s", e)
        finally:
            self._finalize_router()

    def _finalize_cancelled(self):
        if self._finalized:
            return
        self._finalized = True
        try:
            if self._router is not None:
                self._router.recorder.stream_cancelled(
                    error_category=ErrorCategory.CLIENT_CANCELLED
                )
            self._finalize_attempt(success=False, error=GatewayError(
                category=ErrorCategory.CLIENT_CANCELLED,
                type="ClientDisconnected",
                message="stream cancelled by client",
                retryable=False,
            ))
        except Exception as e:
            logger.error("Gateway stream cancel finalize failed: %s", e)
        finally:
            self._finalize_router()

    def _finalize_attempt(self, success: bool, error: Optional[BaseException] = None):
        if self._attempt is None:
            return
        try:
            if not success:
                if isinstance(error, GatewayError):
                    self._attempt.set_error(error)
                elif error is not None:
                    from .errors import classify_error
                    self._attempt.set_error(classify_error(error))
            self._attempt.close()
        except Exception as e:
            logger.error("Gateway stream attempt finalize failed: %s", e)

    def _finalize_router(self):
        try:
            if self._router is not None:
                self._router.close()
        except Exception as e:
            logger.error("Gateway stream router finalize failed: %s", e)

    # ── context manager / delegation ──

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and not self._finalized:
            self._finalize_cancelled()
        else:
            self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._iterable, name)

    def __del__(self):
        if not self._finalized and not self._closed:
            try:
                self._finalize_cancelled()
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
    ):
        self._aiterable = aiterable
        self._router = router
        self._attempt = attempt
        self._handle = runtime_handle
        self._check_done = check_done
        self._started = time.time()
        self._first_token_time: Optional[float] = None
        self._finalized = False
        self._closed = False

        try:
            if router is not None:
                router.recorder.stream_started()
        except Exception as e:
            logger.error("Gateway async stream start event failed: %s", e)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        try:
            chunk = await self._aiterable.__anext__()
            self._record_first_token_if_needed()
            self._check_done_marker(chunk)
            return chunk
        except StopAsyncIteration:
            self._finalize_success()
            raise
        except asyncio.CancelledError:
            self._finalize_cancelled()
            raise
        except GeneratorExit:
            self._finalize_cancelled()
            raise
        except Exception as e:
            self._finalize_error(e)
            raise

    def _record_first_token_if_needed(self):
        if self._first_token_time is not None:
            return
        self._first_token_time = time.time()
        ttft_ms = round((self._first_token_time - self._started) * 1000, 2)
        try:
            if self._router is not None:
                self._router.set_ttft(ttft_ms)
                self._router.recorder.stream_first_token()
            if self._attempt is not None:
                self._attempt.set_upstream_status(ttft_ms=ttft_ms)
        except Exception as e:
            logger.error("Gateway async TTFT recording failed: %s", e)

    def _check_done_marker(self, chunk):
        if not self._check_done:
            return
        try:
            if chunk == "[DONE]":
                self._finalize_success()
        except Exception:
            pass

    async def aclose(self):
        """Async close. Client-cancel semantics (spec §15.3)."""
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
        if not self._finalized:
            self._finalize_cancelled()

    def _finalize_success(self):
        if self._finalized:
            return
        self._finalized = True
        try:
            if self._router is not None:
                self._router.recorder.stream_completed()
            self._finalize_attempt(success=True)
        except Exception as e:
            logger.error("Gateway async stream success finalize failed: %s", e)
        finally:
            self._finalize_router()

    def _finalize_error(self, error: BaseException):
        if self._finalized:
            return
        self._finalized = True
        try:
            if self._router is not None:
                self._router.recorder.response_failed(
                    error_category=ErrorCategory.STREAM_INTERRUPTED
                )
            self._finalize_attempt(success=False, error=error)
        except Exception as e:
            logger.error("Gateway async stream error finalize failed: %s", e)
        finally:
            self._finalize_router()

    def _finalize_cancelled(self):
        if self._finalized:
            return
        self._finalized = True
        try:
            if self._router is not None:
                self._router.recorder.stream_cancelled(
                    error_category=ErrorCategory.CLIENT_CANCELLED
                )
            self._finalize_attempt(success=False, error=GatewayError(
                category=ErrorCategory.CLIENT_CANCELLED,
                type="ClientDisconnected",
                message="stream cancelled by client",
                retryable=False,
            ))
        except Exception as e:
            logger.error("Gateway async stream cancel finalize failed: %s", e)
        finally:
            self._finalize_router()

    def _finalize_attempt(self, success: bool, error: Optional[BaseException] = None):
        if self._attempt is None:
            return
        try:
            if not success:
                if isinstance(error, GatewayError):
                    self._attempt.set_error(error)
                elif error is not None:
                    from .errors import classify_error
                    self._attempt.set_error(classify_error(error))
            self._attempt.close()
        except Exception as e:
            logger.error("Gateway async stream attempt finalize failed: %s", e)

    def _finalize_router(self):
        try:
            if self._router is not None:
                self._router.close()
        except Exception as e:
            logger.error("Gateway async stream router finalize failed: %s", e)

    # ── async context manager ──

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None and not self._finalized:
            self._finalize_cancelled()
        else:
            await self.aclose()
        return False

    def __getattr__(self, name):
        return getattr(self._aiterable, name)

    def __del__(self):
        if not self._finalized and not self._closed:
            try:
                self._finalize_cancelled()
            except Exception:
                pass


def wrap_stream(iterable, router, attempt, runtime_handle=None, check_done: bool = True) -> GatewayStream:
    """Wrap a sync upstream chunk iterator into a GatewayStream."""
    return GatewayStream(iterable, router, attempt, runtime_handle=runtime_handle, check_done=check_done)


def wrap_async_stream(aiterable, router, attempt, runtime_handle=None, check_done: bool = True) -> AsyncGatewayStream:
    """Wrap an async upstream chunk iterator into an AsyncGatewayStream."""
    return AsyncGatewayStream(aiterable, router, attempt, runtime_handle=runtime_handle, check_done=check_done)
