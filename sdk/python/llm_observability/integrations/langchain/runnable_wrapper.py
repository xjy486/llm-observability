"""ObservedLangChainRunnable — wraps any LangChain Runnable with observability.

Creates/reuses an AGENT Root Trace for each invoke/ainvoke/stream/astream.
Injects LangChainObservabilityCallbackHandler into the config.

Spec §4-6: One invocation = one AGENT Trace.
"""
import logging
import time
from typing import Any, Optional

from ...context import get_current_context
from .compat import ensure_langchain_available
from .callback_handler import LangChainObservabilityCallbackHandler
from .runnable_metadata import capture_root_payload

logger = logging.getLogger("llm_obs.integrations.langchain.runnable_wrapper")

_VALID_ROOT_MODES = {"auto", "create", "require_existing"}


def observe_runnable(
    runnable: Any,
    name: str = "runnable",
    root_mode: str = "auto",
) -> "ObservedLangChainRunnable":
    """Wrap a LangChain Runnable with observability.

    Args:
        runnable: Any LangChain Runnable (chain, prompt|model|parser, etc.).
        name: Name for the AGENT span (default 'runnable').
        root_mode: 'auto' (create if no context, reuse if exists),
                   'create' (must be no context),
                   'require_existing' (must have context).

    Returns:
        ObservedLangChainRunnable wrapping the input runnable.
    """
    ensure_langchain_available()

    if root_mode not in _VALID_ROOT_MODES:
        raise ValueError(
            f"Invalid root_mode '{root_mode}'. Must be one of: {_VALID_ROOT_MODES}"
        )

    if not hasattr(runnable, "invoke"):
        raise ValueError(
            "runnable must support invoke(). Pass a LangChain Runnable."
        )

    return ObservedLangChainRunnable(
        runnable=runnable,
        name=name,
        root_mode=root_mode,
    )


def _inject_callback_handler(
    config: Optional[dict],
    handler: LangChainObservabilityCallbackHandler,
) -> dict:
    """Inject the observability callback handler into config.

    Preserves existing callbacks. Never replaces user callbacks.
    Handles: callbacks=None, callbacks=list, callbacks=CallbackManager.
    """
    if config is None:
        config = {}

    config = dict(config)  # shallow copy

    existing = config.get("callbacks")

    if existing is None:
        config["callbacks"] = [handler]
    elif isinstance(existing, list):
        for cb in existing:
            if cb is handler:
                config["callbacks"] = existing
                break
        else:
            config["callbacks"] = existing + [handler]
    else:
        # CallbackManager or other
        try:
            if hasattr(existing, "add_handler"):
                handlers = getattr(existing, "handlers", [])
                if handler not in handlers:
                    existing.add_handler(handler)
                config["callbacks"] = existing
            else:
                config["callbacks"] = [existing, handler]
        except Exception:
            config["callbacks"] = [handler]

    return config


class _RunnableScope:
    """Context manager that creates/reuses an AGENT Root Trace."""

    def __init__(self, name, root_mode, config=None, input=None):
        self._name = name
        self._root_mode = root_mode
        self._config = config
        self._input = input
        self._trace_cm = None
        self._created_trace = False
        self._existing_ctx = None
        self._root_span = None

    def __enter__(self):
        from llm_observability import Observability
        if Observability._tracer is None:
            logger.debug("Observability not initialized — runnable scope is noop")
            return self

        current = get_current_context()

        if self._root_mode == "require_existing":
            if current is None:
                raise RuntimeError(
                    "root_mode='require_existing' but no active trace found."
                )
            self._existing_ctx = current
            return self

        if self._root_mode == "create":
            if current is not None:
                raise RuntimeError(
                    "root_mode='create' but an active trace already exists."
                )

        if current is not None:
            self._existing_ctx = current
            return self

        # Create new trace
        self._trace_cm = Observability.trace(name=self._name)
        self._trace_cm.__enter__()
        self._created_trace = True

        # Override span_name to runnable.<name>
        if self._trace_cm._span is not None:
            self._root_span = self._trace_cm._span
            self._trace_cm._span.span_name = f"runnable.{self._name}"

            from .compat import LANGCHAIN_VERSION
            self._trace_cm._span.set_attribute("framework.name", "langchain")
            self._trace_cm._span.set_attribute("framework.version", LANGCHAIN_VERSION)
            self._trace_cm._span.set_attribute("langchain.component", "runnable")
            self._trace_cm._span.set_attribute("langchain.runnable.name", self._name)

            # Config metadata (sanitized)
            if self._config:
                try:
                    strategy = "masked"
                    if Observability._tracer and Observability._tracer.config:
                        strategy = Observability._tracer.config.payload_strategy
                    from .metadata import sanitize_langchain_config_metadata
                    attrs = sanitize_langchain_config_metadata(self._config, strategy)
                    span = self._trace_cm._span
                    for k, v in attrs.items():
                        span.set_attribute(k, v)
                except Exception as e:
                    logger.debug("Config metadata sanitization failed: %s", e)

            # Capture root input
            if self._input is not None:
                try:
                    strategy = "masked"
                    if Observability._tracer and Observability._tracer.config:
                        strategy = Observability._tracer.config.payload_strategy
                    input_attrs = capture_root_payload(self._input, strategy, "input")
                    for k, v in input_attrs.items():
                        if k == "_payload":
                            if self._trace_cm._span.payload is None:
                                self._trace_cm._span.payload = {}
                            self._trace_cm._span.payload.update(v)
                        else:
                            self._trace_cm._span.set_attribute(k, v)
                except Exception as e:
                    logger.debug("Root input capture failed: %s", e)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._trace_cm is not None and self._created_trace:
            # Capture root output before exiting (if we have it)
            # Output is set via set_root_output
            self._trace_cm.__exit__(exc_type, exc_val, exc_tb)
        return False

    def set_root_output(self, output):
        """Capture root output on the AGENT span."""
        if self._root_span is None:
            return
        try:
            from llm_observability import Observability
            strategy = "masked"
            if Observability._tracer and Observability._tracer.config:
                strategy = Observability._tracer.config.payload_strategy
            output_attrs = capture_root_payload(output, strategy, "output")
            for k, v in output_attrs.items():
                if k == "_payload":
                    if self._root_span.payload is None:
                        self._root_span.payload = {}
                    self._root_span.payload.update(v)
                else:
                    self._root_span.set_attribute(k, v)
        except Exception as e:
            logger.debug("Root output capture failed: %s", e)

    def bind_handler(self, handler: LangChainObservabilityCallbackHandler):
        """Register the root span with the callback handler so chain events
        can be recorded on the AGENT span."""
        if self._root_span is not None:
            handler._root_span = self._root_span
            handler._register_span(self._root_span.span_id, self._root_span)

    @property
    def root_span(self):
        return self._root_span


class ObservedLangChainRunnable:
    """Wraps a LangChain Runnable with automatic AGENT Root Trace creation.

    Delegates invoke/ainvoke/stream/astream with trace lifecycle management.
    Injects LangChainObservabilityCallbackHandler into every call.
    """

    def __init__(
        self,
        runnable: Any,
        name: str = "runnable",
        root_mode: str = "auto",
    ):
        self._runnable = runnable
        self._name = name
        self._root_mode = root_mode

    def _create_handler(self) -> LangChainObservabilityCallbackHandler:
        """Create a fresh handler for each invocation (spec §6)."""
        return LangChainObservabilityCallbackHandler()

    def _prepare_config(self, config, handler):
        """Inject handler and return prepared config."""
        return _inject_callback_handler(config, handler)

    def invoke(self, input, config=None, **kwargs):
        """Synchronous invoke with AGENT trace lifecycle."""
        handler = self._create_handler()
        prepared_config = self._prepare_config(config, handler)

        scope = _RunnableScope(self._name, self._root_mode, config=prepared_config, input=input)
        with scope:
            scope.bind_handler(handler)
            try:
                result = self._runnable.invoke(input, config=prepared_config, **kwargs)
                scope.set_root_output(result)
                return result
            finally:
                handler.close_open_runs(reason="invoke_exit")

    async def ainvoke(self, input, config=None, **kwargs):
        """Async invoke with AGENT trace lifecycle."""
        handler = self._create_handler()
        prepared_config = self._prepare_config(config, handler)

        scope = _RunnableScope(self._name, self._root_mode, config=prepared_config, input=input)
        with scope:
            scope.bind_handler(handler)
            try:
                result = await self._runnable.ainvoke(input, config=prepared_config, **kwargs)
                scope.set_root_output(result)
                return result
            finally:
                handler.close_open_runs(reason="ainvoke_exit")

    def stream(self, input, config=None, **kwargs):
        """Synchronous stream with AGENT trace lifecycle."""
        handler = self._create_handler()
        prepared_config = self._prepare_config(config, handler)

        def _generator():
            scope = _RunnableScope(self._name, self._root_mode, config=prepared_config, input=input)
            with scope:
                scope.bind_handler(handler)
                collected = []
                try:
                    for item in self._runnable.stream(input, config=prepared_config, **kwargs):
                        collected.append(item)
                        yield item
                    if collected:
                        scope.set_root_output(collected[-1] if len(collected) == 1 else collected)
                except GeneratorExit:
                    raise
                finally:
                    handler.close_open_runs(reason="stream_exit")

        return _generator()

    async def astream(self, input, config=None, **kwargs):
        """Async stream with AGENT trace lifecycle."""
        handler = self._create_handler()
        prepared_config = self._prepare_config(config, handler)

        scope = _RunnableScope(self._name, self._root_mode, config=prepared_config, input=input)
        with scope:
            scope.bind_handler(handler)
            collected = []
            try:
                async for item in self._runnable.astream(input, config=prepared_config, **kwargs):
                    collected.append(item)
                    yield item
                if collected:
                    scope.set_root_output(collected[-1] if len(collected) == 1 else collected)
            except GeneratorExit:
                raise
            finally:
                handler.close_open_runs(reason="astream_exit")

    def __getattr__(self, name: str):
        """Transparently delegate unknown attributes to the underlying runnable."""
        return getattr(self._runnable, name)
