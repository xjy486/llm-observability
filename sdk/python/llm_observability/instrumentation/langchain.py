"""LangChain Auto-Instrumentation — Phase 2.5 final closeout (P0-1).

Reliable auto-instrumentation via a per-invocation ContextVar state model
layered on top of class method patching. This fixes:

- Import-order independence (methods resolved at call time on the class).
- Per-invocation isolation: fresh handler/registry/state per root invocation;
  nested calls reuse the root's state (no duplicate AGENT).
- Non-destructive user Config (deep copy before mutating callbacks); the
  copied config is ALWAYS placed back into kwargs["config"] (even when the
  user passed no config), so the callback reaches LangChain.
- Async correctness: ainvoke/astream wrappers await the coroutine INSIDE the
  try/finally, so the AGENT root stays open for the full async execution.
- Subclass coverage: patches invoke/ainvoke/stream/astream on Runnable AND on
  every subclass that overrides these methods in its own __dict__ (e.g.
  BaseChatModel, RunnableSequence, RunnableBinding, RunnableParallel,
  CompiledGraph), so Direct ChatModel / Sequence / Agent invocations enter
  the wrapper.
- Handler cleanup: on root exit, calls handler.close_open_runs() to finalize
  any unfinished callback spans.
"""
import copy
import functools
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass
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


def _collect_candidate_classes():
    """Collect Runnable + subclasses that override invoke/ainvoke/stream/astream.

    Only classes that define the method in their OWN __dict__ (i.e. override the
    base) are returned for a given method, so patching them is necessary.
    """
    candidates = []
    try:
        from langchain_core.runnables.base import Runnable
        candidates.append(Runnable)
    except ImportError:
        return []
    # Subclasses known to override invoke/ainvoke/stream/astream
    for path in (
        "langchain_core.language_models.chat_models.BaseChatModel",
        "langchain_core.language_models.llms.BaseLLM",
        "langchain_core.runnables.base.RunnableSequence",
        "langchain_core.runnables.base.RunnableParallel",
        "langchain_core.runnables.base.RunnableBinding",
        "langchain_core.runnables.base.RunnableBindingBase",
        "langchain_core.runnables.base.RunnableMap",
        "langchain_core.runnables.base.RunnableLambda",
        "langchain_core.runnables.base.RunnablePassthrough",
        # Blocker 1: CompiledStateGraph inherits invoke from Pregel, which DOES
        # define invoke/ainvoke/stream/astream in its own __dict__. Patching
        # Pregel covers create_agent() / CompiledGraph invocations.
        "langgraph.pregel.Pregel",
        "langgraph.graph.state.CompiledStateGraph",
        "langgraph.graph.graph.CompiledGraph",
    ):
        try:
            mod_name, cls_name = path.rsplit(".", 1)
            mod = __import__(mod_name, fromlist=[cls_name])
            cls = getattr(mod, cls_name, None)
            if cls is not None and cls not in candidates:
                candidates.append(cls)
        except Exception:
            pass
    return candidates


class LangChainInstrumentor(BaseInstrumentor):
    """Auto-instruments LangChain by patching Runnable + subclass methods."""

    def __init__(self):
        super().__init__()
        self._tracer = None
        # originals keyed by (class, method_name)
        self._originals: dict[tuple, Any] = {}
        self._patched_classes: list = []
        self._lock = threading.Lock()

    def instrument(self, tracer=None, **kwargs):
        """Install the LangChain auto-instrumentation."""
        if self._patched:
            return
        candidates = _collect_candidate_classes()
        if not candidates:
            logger.warning("langchain_core not installed — cannot auto-instrument")
            return

        self._tracer = tracer
        instrumentor = self

        for cls in candidates:
            for method_name in _PATCHED_METHODS:
                # Only patch if the class defines the method in its OWN __dict__
                # (i.e. it overrides the base). For the base Runnable itself,
                # always patch.
                if cls is not candidates[0] and method_name not in cls.__dict__:
                    continue
                original = cls.__dict__.get(method_name)
                if original is None:
                    continue
                self._originals[(cls, method_name)] = original
                wrapper = instrumentor._make_wrapper(method_name, original)
                try:
                    setattr(cls, method_name, wrapper)
                    if cls not in self._patched_classes:
                        self._patched_classes.append(cls)
                except (TypeError, AttributeError):
                    # Some classes are immutable (e.g. ABCs); skip
                    pass

        self._patched = True
        logger.info(
            "LangChain auto-instrumentation installed (%d classes patched)",
            len(self._patched_classes),
        )

    def _make_wrapper(self, method_name: str, original):
        """Build a wrapper for a Runnable method."""
        instrumentor = self

        if method_name == "invoke":
            @functools.wraps(original)
            def wrapper(self_runnable, *args, **kwargs):
                return instrumentor._run_sync_with_auto_root(
                    original, self_runnable, args, kwargs,
                )
            return wrapper

        if method_name == "ainvoke":
            @functools.wraps(original)
            async def awrapper(self_runnable, *args, **kwargs):
                return await instrumentor._run_async_with_auto_root(
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
        """Enter an invocation. Returns (state, is_root, token)."""
        from ..context import get_current_context
        existing = _AUTO_STATE.get()
        if existing is not None:
            existing.depth += 1
            return existing, False, None

        if get_current_context() is not None:
            state = AutoInvocationState(handler=self._build_callback_handler(), depth=1)
            token = _AUTO_STATE.set(state)
            return state, False, token

        handler = self._build_callback_handler()
        root_cm, _ = self._maybe_open_root()
        state = AutoInvocationState(handler=handler, root_trace_cm=root_cm, depth=1)
        token = _AUTO_STATE.set(state)
        return state, True, token

    def _exit_invocation(self, state, is_root, token, exc_val=None):
        """Exit an invocation. Closes open runs + root, clears ContextVar."""
        if state is None:
            return
        state.depth -= 1
        if is_root or state.depth <= 0:
            # 1.4: close any unfinished callback runs (end/error missing)
            if state.handler is not None:
                try:
                    state.handler.close_open_runs(reason="auto_invocation_exit")
                except Exception:
                    logger.debug("close_open_runs failed", exc_info=True)
            # Close the AGENT root
            self._close_root(state.root_trace_cm, exc_val)
            if token is not None:
                try:
                    _AUTO_STATE.reset(token)
                except Exception:
                    _AUTO_STATE.set(None)
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

    def _normalize_config_to_kwargs(self, args, kwargs):
        """Extract a positional config arg into kwargs.

        LangChain invoke/ainvoke signature: invoke(self, input, config=None, **kwargs).
        When a Sequence calls step.invoke(input, config), config arrives as
        args[1]. We move it into kwargs so _copy_config/_merge_callback can
        operate on it without creating a duplicate positional+keyword conflict.

        Returns (new_args, kwargs_with_config).
        """
        if len(args) >= 2 and "config" not in kwargs:
            # args layout after self_runnable: (input, config, ...)
            kwargs = {**kwargs, "config": args[1]}
            args = (args[0],) + args[2:]
        return args, kwargs

    def _copy_config(self, kwargs):
        """Non-destructive Config copy. ALWAYS places the copy back into kwargs.

        Blocker 2: SHALLOW copy only — never deepcopy. Deepcopying would clone
        user Callback handler instances, breaking stateful callbacks (the user's
        original handler would never receive events; only the clone would). We
        shallow-copy the config dict and the callbacks list so handler object
        identity is preserved while the config itself is safe to mutate.
        """
        config = kwargs.get("config")
        if config is None:
            config_copy = {}
            new_kwargs = {**kwargs, "config": config_copy}
            return new_kwargs, config_copy
        # Shallow copy the config dict (preserve handler object references)
        if isinstance(config, dict):
            config_copy = dict(config)
            # Shallow-copy the callbacks list if present (keep handler refs)
            cbs = config_copy.get("callbacks")
            if isinstance(cbs, list):
                config_copy["callbacks"] = list(cbs)
        else:
            # Non-dict config (e.g. RunnableConfig dataclass) — best-effort copy
            try:
                config_copy = dict(config)
            except Exception:
                config_copy = {}
        new_kwargs = {**kwargs, "config": config_copy}
        return new_kwargs, config_copy

    def _merge_callback(self, config_copy, handler):
        """Merge our callback handler into the COPIED config (non-destructive).

        Preserves user callbacks: None / list / CallbackManager / AsyncCallbackManager.
        Blocker 2: CallbackManager is CLONED via .copy() (preserves handler identity
        + full semantics: inheritable_handlers/tags/metadata/parent_run_id), then our
        handler is added. User handler instances are never copied.
        """
        if handler is None:
            return
        if not isinstance(config_copy, dict):
            return
        existing = config_copy.get("callbacks")
        if existing is None:
            config_copy["callbacks"] = [handler]
            return
        # CallbackManager / AsyncCallbackManager — clone + add handler (identity preserved)
        if hasattr(existing, "handlers") and hasattr(existing, "copy") and not isinstance(existing, list):
            try:
                cloned = existing.copy()
                cloned.add_handler(handler, inherit=False)
                config_copy["callbacks"] = cloned
                return
            except Exception:
                logger.debug("CallbackManager clone failed; falling back", exc_info=True)
        if isinstance(existing, list):
            if not any(isinstance(c, type(handler)) for c in existing):
                config_copy["callbacks"] = existing + [handler]
        else:
            # Single handler object — wrap alongside (preserve reference)
            config_copy["callbacks"] = [existing, handler]

    # ── Sync invocation ──

    def _run_sync_with_auto_root(self, original, self_runnable, args, kwargs):
        """Run a sync invocation with per-invocation state."""
        state, is_root, token = self._enter_invocation()
        args, kwargs = self._normalize_config_to_kwargs(args, kwargs)
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

    # ── Async invocation (1.2 fix: await INSIDE try/finally) ──

    async def _run_async_with_auto_root(self, original, self_runnable, args, kwargs):
        """Run an async invocation with per-invocation state.

        The coroutine is awaited INSIDE the try/finally so the AGENT root and
        auto state stay open for the full async execution (not closed before
        the coroutine runs).
        """
        state, is_root, token = self._enter_invocation()
        args, kwargs = self._normalize_config_to_kwargs(args, kwargs)
        kwargs, config_copy = self._copy_config(kwargs)
        self._merge_callback(config_copy, state.handler if state else None)
        exc_val = None
        try:
            return await original(self_runnable, *args, **kwargs)
        except BaseException as e:
            exc_val = e
            raise
        finally:
            self._exit_invocation(state, is_root, token, exc_val)

    # ── Streaming ──

    def _stream_sync_with_auto_root(self, original, self_runnable, args, kwargs):
        state, is_root, token = self._enter_invocation()
        args, kwargs = self._normalize_config_to_kwargs(args, kwargs)
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
        state, is_root, token = self._enter_invocation()
        args, kwargs = self._normalize_config_to_kwargs(args, kwargs)
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
        """Restore original methods on all patched classes (full recovery)."""
        if not self._patched:
            return
        with self._lock:
            for (cls, method_name), original in self._originals.items():
                try:
                    setattr(cls, method_name, original)
                except Exception:
                    pass
            self._originals.clear()
            self._patched_classes.clear()
            self._tracer = None
            self._patched = False
        _AUTO_STATE.set(None)
        logger.info("LangChain auto-instrumentation removed (all classes restored)")
