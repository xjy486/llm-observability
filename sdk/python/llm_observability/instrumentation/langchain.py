"""LangChain Auto-Instrumentation — Phase 2.5 final closeout (P0-1).

Reliable auto-instrumentation via a per-invocation ContextVar state model
layered on top of Runnable base-class patching. This fixes:

- Import-order independence (methods resolved at call time on the class).
- Per-invocation isolation: fresh handler/registry/state per root invocation;
  nested calls reuse the root's state (no duplicate AGENT).
- Non-destructive user Config (deep copy before mutating callbacks).
- User callback preservation (list / CallbackManager / AsyncCallbackManager).
- Hard dedup vs observe_runnable / observe_agent / middleware / OpenAI.

The ContextVar holds `AutoInvocationState(handler, root_trace_cm, depth)`.
Root call (no active trace, depth 0): create state. Nested (depth>0 or active
trace): reuse, depth++. Exit root: close runs, end AGENT, clear ContextVar.
"""
import copy
import functools
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import BaseInstrumentor

logger = logging.getLogger("llm_obs.instrumentation.langchain")

_PATCHED_METHODS = ("invoke", "ainvoke", "stream", "astream")


@dataclass
class AutoInvocationState:
    """Per-root-invocation state held in a ContextVar."""
    handler: Any = None
    root_trace_cm: Any = None
    depth: int = 0


_AUTO_STATE: ContextVar[Optional[AutoInvocationState]] = ContextVar(
    "llm_obs_langchain_auto_state", default=None
)


class LangChainInstrumentor(BaseInstrumentor):
    """Auto-instruments LangChain by patching Runnable base-class methods."""

    def __init__(self):
        super().__init__()
        self._tracer = None
        self._originals: dict[str, Any] = {}
        self._runnable_cls = None
        self._lock = threading.Lock()

    def instrument(self, tracer=None, **kwargs):
        """Install the LangChain auto-instrumentation."""
        if self._patched:
            return
        try:
            from langchain_core.runnables.base import Runnable
        except ImportError:
            logger.warning("langchain_core not installed — cannot auto-instrument")
            return

        self._tracer = tracer
        self._runnable_cls = Runnable
        instrumentor = self

        for method_name in _PATCHED_METHODS:
            original = getattr(Runnable, method_name, None)
            if original is None:
                continue
            self._originals[method_name] = original
            wrapper = instrumentor._make_wrapper(method_name, original)
            setattr(Runnable, method_name, wrapper)

        self._patched = True
        logger.info("LangChain auto-instrumentation installed (Runnable patch)")

    def _make_wrapper(self, method_name: str, original):
        """Build a wrapper for a Runnable method."""
        instrumentor = self

        if method_name == "invoke":
            @functools.wraps(original)
            def wrapper(self_runnable, *args, **kwargs):
                return instrumentor._run_with_auto_root(
                    original, self_runnable, args, kwargs,
                )
            return wrapper

        if method_name == "ainvoke":
            @functools.wraps(original)
            async def awrapper(self_runnable, *args, **kwargs):
                return await instrumentor._run_with_auto_root(
                    original, self_runnable, args, kwargs,
                )
            return awrapper

        if method_name == "stream":
            @functools.wraps(original)
            def swrapper(self_runnable, *args, **kwargs):
                yield from instrumentor._stream_sync_with_auto_root(
                    original, self_runnable, args, kwargs,
                )
            return swrapper

        if method_name == "astream":
            @functools.wraps(original)
            async def aswrapper(self_runnable, *args, **kwargs):
                async for item in instrumentor._stream_async_with_auto_root(
                    original, self_runnable, args, kwargs,
                ):
                    yield item
            return aswrapper

        return original

    # ── Per-invocation state management ──

    def _enter_invocation(self):
        """Enter an invocation. Returns (state, is_root, token).

        Root invocation (no active trace, no existing state): creates a fresh
        handler + AGENT root, sets the ContextVar. Nested: reuses state,
        increments depth.
        """
        from ..context import get_current_context
        existing = _AUTO_STATE.get()
        if existing is not None:
            existing.depth += 1
            return existing, False, None

        if get_current_context() is not None:
            # A trace already exists (e.g., user @agent) — don't auto-create root
            state = AutoInvocationState(handler=self._build_callback_handler(), depth=1)
            token = _AUTO_STATE.set(state)
            return state, False, token

        # Root invocation: create handler + AGENT root
        handler = self._build_callback_handler()
        root_cm, _ = self._maybe_open_root()
        state = AutoInvocationState(handler=handler, root_trace_cm=root_cm, depth=1)
        token = _AUTO_STATE.set(state)
        return state, True, token

    def _exit_invocation(self, state, is_root, token, exc_val=None):
        """Exit an invocation. Closes root on root exit, clears ContextVar."""
        if state is None:
            return
        state.depth -= 1
        if is_root or state.depth <= 0:
            # Root exit: close open runs, end AGENT, clear ContextVar
            self._close_root(state.root_trace_cm, exc_val)
            if token is not None:
                _AUTO_STATE.reset(token)
            else:
                _AUTO_STATE.set(None)

    def _build_callback_handler(self):
        """Create a FRESH callback handler per invocation (P0-1 isolation)."""
        try:
            from ..integrations.langchain.callback_handler import (
                LangChainObservabilityCallbackHandler,
            )
            return LangChainObservabilityCallbackHandler()
        except Exception:
            logger.debug("callback handler creation failed", exc_info=True)
            return None

    def _maybe_open_root(self):
        """Open an AGENT root trace if no active trace exists."""
        from ..context import get_current_context
        if get_current_context() is not None:
            return None, None
        try:
            from llm_observability import Observability
            if Observability._tracer is None or not Observability._initialized:
                return None, None
            trace_cm = Observability.trace(name="langchain.auto")
            span = trace_cm.__enter__()
            return trace_cm, span
        except Exception:
            logger.debug("auto-root trace creation failed", exc_info=True)
            return None, None

    def _close_root(self, trace_cm, exc_val=None):
        """Close an auto-created root trace."""
        if trace_cm is None:
            return
        try:
            if exc_val is not None:
                trace_cm.__exit__(type(exc_val), exc_val, exc_val.__traceback__)
            else:
                trace_cm.__exit__(None, None, None)
        except Exception:
            logger.debug("auto-root trace close failed", exc_info=True)

    def _copy_config(self, kwargs):
        """Non-destructive Config copy (P0-1): never mutate user Config."""
        config = kwargs.get("config")
        if config is None:
            return kwargs, {}
        try:
            config_copy = copy.deepcopy(config)
        except Exception:
            # Fallback: shallow dict copy
            try:
                config_copy = dict(config) if isinstance(config, dict) else {}
            except Exception:
                config_copy = {}
        new_kwargs = {**kwargs, "config": config_copy}
        return new_kwargs, config_copy

    def _merge_callback(self, config_copy, handler):
        """Merge our callback handler into the COPIED config (non-destructive).

        Preserves user callbacks: None / list / CallbackManager / AsyncCallbackManager.
        """
        if handler is None:
            return
        if not isinstance(config_copy, dict):
            return
        existing = config_copy.get("callbacks")
        if existing is None:
            config_copy["callbacks"] = [handler]
        elif isinstance(existing, list):
            if not any(isinstance(c, type(handler)) for c in existing):
                config_copy["callbacks"] = existing + [handler]
        else:
            # CallbackManager / AsyncCallbackManager — wrap alongside (don't replace)
            config_copy["callbacks"] = [existing, handler]

    def _run_with_auto_root(self, original, self_runnable, args, kwargs):
        """Run a non-streaming invocation with per-invocation state."""
        state, is_root, token = self._enter_invocation()
        kwargs, config_copy = self._copy_config(kwargs)
        self._merge_callback(config_copy, state.handler if state else None)
        exc_val = None
        try:
            return original(self_runnable, *args, **kwargs)
        except BaseException as e:
            exc_val = e
            raise
        finally:
            self._exit_invocation(state, is_root, token, exc_val)

    def _stream_sync_with_auto_root(self, original, self_runnable, args, kwargs):
        """Run a sync streaming invocation with per-invocation state."""
        state, is_root, token = self._enter_invocation()
        kwargs, config_copy = self._copy_config(kwargs)
        self._merge_callback(config_copy, state.handler if state else None)
        exc_val = None
        try:
            for item in original(self_runnable, *args, **kwargs):
                yield item
        except BaseException as e:
            exc_val = e
            raise
        finally:
            self._exit_invocation(state, is_root, token, exc_val)

    async def _stream_async_with_auto_root(self, original, self_runnable, args, kwargs):
        """Run an async streaming invocation with per-invocation state."""
        state, is_root, token = self._enter_invocation()
        kwargs, config_copy = self._copy_config(kwargs)
        self._merge_callback(config_copy, state.handler if state else None)
        exc_val = None
        try:
            async for item in original(self_runnable, *args, **kwargs):
                yield item
        except BaseException as e:
            exc_val = e
            raise
        finally:
            self._exit_invocation(state, is_root, token, exc_val)

    def uninstrument(self):
        """Restore original Runnable methods (full recovery on shutdown)."""
        if not self._patched:
            return
        with self._lock:
            for method_name, original in self._originals.items():
                if self._runnable_cls is not None and hasattr(self._runnable_cls, method_name):
                    try:
                        setattr(self._runnable_cls, method_name, original)
                    except Exception:
                        pass
            self._originals.clear()
            self._runnable_cls = None
            self._tracer = None
            self._patched = False
        # Clear any lingering invocation state
        _AUTO_STATE.set(None)
        logger.info("LangChain auto-instrumentation removed (Runnable restored)")
