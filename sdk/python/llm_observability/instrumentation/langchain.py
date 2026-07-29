"""LangChain Auto-Instrumentation — Phase 2.5 (P0-1 refactor).

Patches the ``Runnable`` base class methods (invoke/ainvoke/stream/astream)
to inject a fresh callback handler per invocation. This approach:

- Is independent of LangChain import order (methods are looked up at call
  time on the class, not via a bound ``from ... import`` reference).
- Creates a NEW ``LangChainObservabilityCallbackHandler`` + Auto-Root state
  per root invocation (per-invocation isolation — no cross-request state).
- Does NOT modify the user's CallbackManager, does NOT replace user
  callbacks (we MERGE our handler alongside theirs), does NOT patch any
  Runnable INSTANCE (only the base class methods, reversibly).
- Auto-creates an AGENT root trace on the outermost invocation when no
  active trace exists.

Dedup (spec §5): the callback LLM span sets logical_llm_span_active=True so
the OpenAI instrumentor skips a second LLM span (only GATEWAY remains).
"""
import functools
import logging
import threading
from typing import Any, Optional

from .base import BaseInstrumentor

logger = logging.getLogger("llm_obs.instrumentation.langchain")

_PATCHED_METHODS = ("invoke", "ainvoke", "stream", "astream")


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
                    original, self_runnable, args, kwargs, is_async=False, is_stream=False,
                )
            return wrapper

        if method_name == "ainvoke":
            @functools.wraps(original)
            async def awrapper(self_runnable, *args, **kwargs):
                return await instrumentor._run_with_auto_root(
                    original, self_runnable, args, kwargs, is_async=True, is_stream=False,
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

    # ── Auto-root + callback injection ──

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
        """Open an AGENT root trace if no active trace exists.

        Returns (trace_cm, span) or (None, None).
        """
        from ..context import get_current_context
        if get_current_context() is not None:
            return None, None  # a trace already exists — don't auto-create
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

    def _merge_callback(self, kwargs, handler):
        """Merge our callback handler into the invocation config (non-destructive)."""
        if handler is None:
            return kwargs
        config = kwargs.get("config")
        if config is None:
            config = {}
            kwargs["config"] = config
        # Don't mutate a frozen/mapping the user passed — copy to a dict
        if not isinstance(config, dict):
            try:
                config = dict(config)
                kwargs["config"] = config
            except Exception:
                return kwargs
        existing = config.get("callbacks")
        if existing is None:
            config["callbacks"] = [handler]
        elif isinstance(existing, list):
            # Don't add duplicates of our handler type
            if not any(isinstance(c, type(handler)) for c in existing):
                config["callbacks"] = existing + [handler]
        else:
            # User passed a CallbackManager — do NOT modify it; wrap in a list
            # alongside so user callbacks are preserved.
            config["callbacks"] = [existing, handler]
        return kwargs

    def _run_with_auto_root(self, original, self_runnable, args, kwargs, is_async, is_stream):
        """Run a non-streaming invocation with auto-root + callback injection."""
        handler = self._build_callback_handler()
        trace_cm, _ = self._maybe_open_root()
        kwargs = self._merge_callback(kwargs, handler)
        exc_val = None
        try:
            return original(self_runnable, *args, **kwargs)
        except BaseException as e:
            exc_val = e
            raise
        finally:
            self._close_root(trace_cm, exc_val)

    def _stream_sync_with_auto_root(self, original, self_runnable, args, kwargs):
        """Run a sync streaming invocation with auto-root + callback injection.

        The root trace stays open for the full duration of the generator.
        """
        handler = self._build_callback_handler()
        trace_cm, _ = self._maybe_open_root()
        kwargs = self._merge_callback(kwargs, handler)
        exc_val = None
        try:
            for item in original(self_runnable, *args, **kwargs):
                yield item
        except BaseException as e:
            exc_val = e
            raise
        finally:
            self._close_root(trace_cm, exc_val)

    async def _stream_async_with_auto_root(self, original, self_runnable, args, kwargs):
        """Run an async streaming invocation with auto-root + callback injection.

        The root trace stays open for the full duration of the generator.
        """
        handler = self._build_callback_handler()
        trace_cm, _ = self._maybe_open_root()
        kwargs = self._merge_callback(kwargs, handler)
        exc_val = None
        try:
            async for item in original(self_runnable, *args, **kwargs):
                yield item
        except BaseException as e:
            exc_val = e
            raise
        finally:
            self._close_root(trace_cm, exc_val)

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
        logger.info("LangChain auto-instrumentation removed (Runnable restored)")
