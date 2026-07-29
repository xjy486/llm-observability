"""LangChain Auto-Instrumentation — Phase 2.5.

Implements the LangChainInstrumentor that, on init(auto_instrument_langchain=True),
automatically captures LangChain runs without explicit wrappers.

Strategy (spec §5, priority order):
    1. LangChain official Callback/Config extension point — patch
       ``langchain_core.runnables.config.ensure_config`` to inject a default
       callback handler. This does NOT modify the user's CallbackManager, does
       NOT replace user callbacks (we only add to defaults when none set), and
       does NOT patch any Runnable instance.
    2. A root-creating callback handler subclass auto-creates the AGENT root
       trace on the first chain run when no active trace exists.

Dedup (spec §5): the callback LLM span sets logical_llm_span_active=True so the
OpenAI instrumentor skips creating a second LLM span (only GATEWAY remains).
"""
import logging
import threading
from typing import Any, Optional

from .base import BaseInstrumentor
from ..context import get_current_context

logger = logging.getLogger("llm_obs.instrumentation.langchain")


class LangChainAutoRootHandler:
    """Callback handler that auto-creates an AGENT root trace.

    Wraps the existing LangChainObservabilityCallbackHandler: on the ROOT
    chain run (parent_run_id is None), opens an Observability trace if no
    active context exists, then delegates all callbacks to the inner handler.
    On root chain end/error, closes the auto-created trace.
    """

    raise_error: bool = False
    run_inline: bool = True

    def __init__(self, inner):
        self._inner = inner
        self._lock = threading.RLock()
        self._auto_trace = None  # the TraceContextManager we created
        self._root_run_id: Optional[str] = None

    def _maybe_open_root(self, parent_run_id: Optional[Any], run_id: Any):
        """On the root run, open an AGENT trace if none active."""
        if parent_run_id is not None:
            return
        with self._lock:
            if self._auto_trace is not None:
                return
            if get_current_context() is not None:
                # A trace already exists — don't auto-create
                return
            try:
                from llm_observability import Observability
                if Observability._tracer is None or not Observability._initialized:
                    return
                self._auto_trace = Observability.trace(name="langchain.auto")
                self._auto_trace.__enter__()
                self._root_run_id = str(run_id)
            except Exception:
                logger.debug("auto-root trace creation failed", exc_info=True)
                self._auto_trace = None

    def _maybe_close_root(self, run_id: Any, exc_val: Optional[BaseException] = None):
        """Close the auto-created trace on root run end."""
        with self._lock:
            if self._auto_trace is None:
                return
            if self._root_run_id is not None and str(run_id) != self._root_run_id:
                return
            trace = self._auto_trace
            self._auto_trace = None
            self._root_run_id = None
        try:
            if exc_val is not None:
                trace.__exit__(type(exc_val), exc_val, exc_val.__traceback__)
            else:
                trace.__exit__(None, None, None)
        except Exception:
            logger.debug("auto-root trace close failed", exc_info=True)

    # ── Chain callbacks ──
    def on_chain_start(self, serialized, inputs, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._maybe_open_root(parent_run_id, run_id)
        return self._inner.on_chain_start(serialized, inputs, run_id, parent_run_id, tags, metadata, **kwargs)

    def on_chain_end(self, outputs, run_id, parent_run_id=None, **kwargs):
        try:
            self._inner.on_chain_end(outputs, run_id, parent_run_id, **kwargs)
        finally:
            self._maybe_close_root(run_id)

    def on_chain_error(self, error, run_id, parent_run_id=None, **kwargs):
        try:
            self._inner.on_chain_error(error, run_id, parent_run_id, **kwargs)
        finally:
            self._maybe_close_root(run_id, error)

    # ── LLM callbacks ──
    def on_llm_start(self, serialized, prompts, run_id, parent_run_id=None, **kwargs):
        return self._inner.on_llm_start(serialized, prompts, run_id, parent_run_id, **kwargs)

    def on_llm_end(self, response, run_id, parent_run_id=None, **kwargs):
        return self._inner.on_llm_end(response, run_id, parent_run_id, **kwargs)

    def on_llm_error(self, error, run_id, parent_run_id=None, **kwargs):
        return self._inner.on_llm_error(error, run_id, parent_run_id, **kwargs)

    def on_llm_new_token(self, token, run_id, parent_run_id=None, **kwargs):
        return self._inner.on_llm_new_token(token, run_id, parent_run_id, **kwargs)

    # ── Tool callbacks ──
    def on_tool_start(self, serialized, input_str, run_id, parent_run_id=None, **kwargs):
        return self._inner.on_tool_start(serialized, input_str, run_id, parent_run_id, **kwargs)

    def on_tool_end(self, output, run_id, parent_run_id=None, **kwargs):
        return self._inner.on_tool_end(output, run_id, parent_run_id, **kwargs)

    def on_tool_error(self, error, run_id, parent_run_id=None, **kwargs):
        return self._inner.on_tool_error(error, run_id, parent_run_id, **kwargs)

    # ── Delegation for any other attributes ──
    def __getattr__(self, name):
        return getattr(self._inner, name)


class LangChainInstrumentor(BaseInstrumentor):
    """Auto-instruments LangChain by injecting a default callback handler.

    Patches ``langchain_core.runnables.config.ensure_config`` to merge our
    callback handler into the default callbacks when the user has not provided
    any. Reversible on uninstrument().

    Does NOT modify user CallbackManager, replace user callbacks, or patch
    any Runnable instance.
    """

    _HANDLER_SENTINEL = "_llm_observability_auto_handler"

    def __init__(self):
        super().__init__()
        self._tracer = None
        self._original_ensure_config = None
        self._handler = None

    def instrument(self, tracer=None, **kwargs):
        """Install the LangChain auto-instrumentation."""
        if self._patched:
            return
        try:
            import langchain_core.runnables.config as _config_mod
        except ImportError:
            logger.warning("langchain_core not installed — cannot auto-instrument")
            return

        from ..integrations.langchain.callback_handler import LangChainObservabilityCallbackHandler
        self._tracer = tracer
        inner = LangChainObservabilityCallbackHandler()
        self._handler = LangChainAutoRootHandler(inner)

        self._original_ensure_config = _config_mod.ensure_config

        instrumentor = self

        def _patched_ensure_config(*args, **kw):
            config = instrumentor._original_ensure_config(*args, **kw)
            try:
                config = instrumentor._merge_default_callback(config)
            except Exception:
                logger.debug("ensure_config callback merge failed", exc_info=True)
            return config

        _config_mod.ensure_config = _patched_ensure_config
        self._patched = True
        logger.info("LangChain auto-instrumentation installed")

    def _merge_default_callback(self, config: dict) -> dict:
        """Merge our auto callback handler into config callbacks if absent."""
        if self._handler is None:
            return config
        callbacks = config.get("callbacks")
        if callbacks is None:
            config["callbacks"] = [self._handler]
            return config
        # If user provided callbacks, do NOT replace them; only add ours if absent
        try:
            existing = list(callbacks) if not isinstance(callbacks, list) else callbacks
        except TypeError:
            existing = [callbacks]
        already = any(
            getattr(cb, "_llm_obs_auto", False) or isinstance(cb, LangChainAutoRootHandler)
            for cb in existing
        )
        if already:
            return config
        # Append our handler to the user's list (don't replace user callbacks)
        config["callbacks"] = existing + [self._handler]
        return config

    def uninstrument(self):
        """Restore original ensure_config."""
        if not self._patched:
            return
        try:
            import langchain_core.runnables.config as _config_mod
            if self._original_ensure_config is not None:
                _config_mod.ensure_config = self._original_ensure_config
        except ImportError:
            pass
        self._original_ensure_config = None
        self._handler = None
        self._tracer = None
        self._patched = False
        logger.info("LangChain auto-instrumentation removed")
