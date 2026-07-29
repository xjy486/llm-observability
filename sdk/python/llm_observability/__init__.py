"""LLM Observability Application SDK.

Public API:
    Observability.init(...)   — Initialize SDK
    Observability.trace(...)  — Create a business task Trace
    Observability.tool(...)   — Create a Tool span (Phase 2.2)
    Observability.task(...)   — Create a Task span (Phase 2.5)
    Observability.annotate(...) — Annotate current/explicit span (Phase 2.5)
    Observability.set_association_properties(...) / association_context(...) (Phase 2.5)
    Observability.inject_carrier / extract_carrier (Phase 2.5)
    Observability.track_task_client_call / track_agent_server_call (Phase 2.5)

Decorators (Phase 2.5):
    from llm_observability.decorators import agent, chain, task, tool, llm

P0-1: Reporter lifecycle is auto-managed via a background thread.
P0-2: OpenAI Instrumentor is held as a single instance for correct lifecycle.
P1-1: Nested trace() raises an error.
P1-2: sample_rate and api_key are enforced.
"""
import atexit
import logging
import random
import threading
from typing import Optional, Any

from .config import Config
from .reporter import Reporter
from .tracer import Tracer, TraceContextManager
from .context import get_current_context

__version__ = "0.1.0"

logger = logging.getLogger("llm_obs")


class Observability:
    """Public SDK entry point.

    Usage:
        Observability.init(app_name="my-app", endpoint="http://localhost:8001")
        with Observability.trace(name="my-task"):
            # ... business code ...
        Observability.shutdown()
    """

    _tracer: Optional[Tracer] = None
    _reporter: Optional[Reporter] = None
    _config: Optional[Config] = None
    _initialized: bool = False
    _openai_instrumentor = None  # P0-2: single instance
    _instrument_manager = None  # Phase 2.5: manages all instrumentors
    _lock = threading.Lock()
    _atexit_registered = False

    @classmethod
    def init(
        cls,
        app_name: str = "unknown",
        endpoint: str = "http://localhost:8001",
        api_key: Optional[str] = None,
        payload_strategy: str = "masked",
        sample_rate: float = 1.0,
        auto_instrument_openai: bool = True,
        auto_instrument_langchain: bool = False,
        capture_retriever_content: bool = False,
        block_instruments: Optional[set] = None,
        max_attribute_bytes: int = 8 * 1024,
        max_payload_bytes: int = 32 * 1024,
        fail_open: bool = True,
    ):
        """Initialize the SDK. Idempotent — safe to call multiple times.

        Phase 2.5 additions:
            auto_instrument_langchain: Auto-instrument LangChain on init.
            block_instruments: set of Instruments enum values to block.
            max_attribute_bytes / max_payload_bytes: size limits.
            fail_open: telemetry errors never block business.

        P0-1: Auto-starts the Reporter in a background thread.
        P0-2: Creates and holds a single OpenAIInstrumentor instance.
        """
        with cls._lock:
            if cls._initialized:
                logger.warning("Observability.init() already called — skipping (idempotent)")
                return

            # P1-1: validate config ranges
            if not (1024 <= max_attribute_bytes <= 128 * 1024):
                raise ValueError(
                    f"max_attribute_bytes must be between 1 KiB and 128 KiB, got {max_attribute_bytes}"
                )
            if max_payload_bytes < 1024:
                raise ValueError(
                    f"max_payload_bytes must be >= 1 KiB, got {max_payload_bytes}"
                )
            if max_payload_bytes > 16 * 1024 * 1024:
                raise ValueError(
                    f"max_payload_bytes must be <= 16 MiB, got {max_payload_bytes}"
                )
            if not (0.0 <= sample_rate <= 1.0):
                raise ValueError(f"sample_rate must be between 0.0 and 1.0, got {sample_rate}")

            cls._config = Config(
                app_name=app_name,
                endpoint=endpoint,
                api_key=api_key,
                payload_strategy=payload_strategy,
                sample_rate=sample_rate,
                auto_instrument_openai=auto_instrument_openai,
                auto_instrument_langchain=auto_instrument_langchain,
                capture_retriever_content=capture_retriever_content,
                max_attribute_bytes=max_attribute_bytes,
                max_payload_bytes=max_payload_bytes,
                fail_open=fail_open,
            )

            # P0-1: Reporter with api_key for auth (P1-2)
            cls._reporter = Reporter(endpoint=endpoint, api_key=api_key)
            cls._tracer = Tracer(config=cls._config, reporter=cls._reporter)

            # P0-1: Auto-start Reporter in background thread
            cls._reporter.start_sync()

            # Phase 2.5: Unified InstrumentManager
            from .instruments import InstrumentManager
            cls._instrument_manager = InstrumentManager(cls._tracer)
            cls._instrument_manager.set_blocked(block_instruments)

            # P0-2: Use a single instrumentor instance via the manager
            if auto_instrument_openai:
                cls._instrument_openai()

            # Phase 2.5: Auto LangChain
            if auto_instrument_langchain:
                cls._instrument_langchain()

            cls._initialized = True

            # Register atexit for best-effort flush
            if not cls._atexit_registered:
                atexit.register(cls._atexit_handler)
                cls._atexit_registered = True

            logger.info("Observability SDK initialized, app=%s", app_name)

    @classmethod
    def trace(
        cls,
        name: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        business_scene: Optional[str] = None,
    ) -> TraceContextManager:
        """Create a new business task Trace with an AGENT root span.

        P1-1: Raises RuntimeError if nested inside an existing trace.

        Must call Observability.init() first.

        Args:
            name: Task name.
            session_id: Optional session ID.
            user_id: Optional user ID.
            business_scene: Optional business scene.

        Returns:
            TraceContextManager for the AGENT span.
        """
        if not cls._initialized or cls._tracer is None:
            raise RuntimeError("Observability.init() must be called before trace()")

        # P1-1: Reject nested trace()
        current = get_current_context()
        if current is not None:
            raise RuntimeError(
                "Nested trace() is not allowed — an active trace already exists. "
                "Use agent()/tool()/span() to create child spans within a trace."
            )

        return cls._tracer.trace(
            name=name,
            session_id=session_id,
            user_id=user_id,
            business_scene=business_scene,
        )

    @classmethod
    def tool(
        cls,
        name: str,
        tool_type: Optional[str] = None,
        input: Any = None,
        call_id: Optional[str] = None,
        attributes: Optional[dict] = None,
    ):
        """Create a Tool span within the current trace (Phase 2.2).

        Requires an active trace (Observability.trace()).

        Args:
            name: Tool name (e.g. 'web_search'). Required.
            tool_type: Optional type (e.g. 'search', 'http').
            input: Optional input payload.
            call_id: Optional LLM tool call ID for logical association.
            attributes: Optional extra attributes.
        """
        if not cls._initialized or cls._tracer is None:
            raise RuntimeError("Observability.init() must be called before tool()")
        return cls._tracer.tool(
            name=name, tool_type=tool_type, input=input,
            call_id=call_id, attributes=attributes,
        )

    @classmethod
    def instrument_tool(
        cls,
        name: str,
        tool_type: Optional[str] = None,
    ):
        """Decorator that wraps a function with a TOOL span (Phase 2.2).

        P0-1 fix: This decorator can be defined BEFORE Observability.init().
        The SDK initialization check is deferred to function call time.

        Captures function arguments as input, return value as output.
        Supports both sync and async functions.

        Error semantics:
        - Definition time: no error (decorator just returns a wrapper)
        - Call time before init: RuntimeError("init() must be called...")
        - Call time without active trace: RuntimeError("requires an active trace")
        """
        import functools
        import inspect as _inspect

        def decorator(func):
            is_async = _inspect.iscoroutinefunction(func)

            if is_async:
                @functools.wraps(func)
                async def async_wrapper(*args, **kwargs):
                    if not cls._initialized or cls._tracer is None:
                        raise RuntimeError(
                            "Observability.init() must be called before invoking an instrumented tool"
                        )
                    bound_input = _bind_decorator_args(func, args, kwargs)
                    with cls._tracer.tool(
                        name=name, tool_type=tool_type, input=bound_input,
                    ) as tool:
                        result = await func(*args, **kwargs)
                        tool.set_output(result)
                        return result
                return async_wrapper
            else:
                @functools.wraps(func)
                def sync_wrapper(*args, **kwargs):
                    if not cls._initialized or cls._tracer is None:
                        raise RuntimeError(
                            "Observability.init() must be called before invoking an instrumented tool"
                        )
                    bound_input = _bind_decorator_args(func, args, kwargs)
                    with cls._tracer.tool(
                        name=name, tool_type=tool_type, input=bound_input,
                    ) as tool:
                        result = func(*args, **kwargs)
                        tool.set_output(result)
                        return result
                return sync_wrapper

        return decorator

    @classmethod
    def langchain_middleware(cls):
        """Create a LangChainObservabilityMiddleware instance.

        Convenience method that returns a middleware for use with
        create_agent(middleware=[...]).

        Must call Observability.init() first.
        """
        if not cls._initialized or cls._tracer is None:
            raise RuntimeError("Observability.init() must be called before langchain_middleware()")
        from .integrations.langchain.middleware import LangChainObservabilityMiddleware
        return LangChainObservabilityMiddleware()

    @classmethod
    def instrument_langchain_agent(
        cls,
        agent,
        name: str = "langchain.agent",
        root_mode: str = "auto",
        session_id=None,
        user_id=None,
        business_scene=None,
    ):
        """Wrap a LangChain agent with observability.

        Convenience method that calls observe_agent() under the hood.
        Must call Observability.init() first.
        """
        if not cls._initialized or cls._tracer is None:
            raise RuntimeError("Observability.init() must be called before instrument_langchain_agent()")
        from .integrations.langchain.agent_wrapper import observe_agent
        return observe_agent(
            agent,
            name=name,
            root_mode=root_mode,
            session_id=session_id,
            user_id=user_id,
            business_scene=business_scene,
        )

    @classmethod
    def observe_runnable(
        cls, runnable, name="runnable", root_mode="auto",
        session_id=None, user_id=None, business_scene=None,
    ):
        """Wrap a LangChain Runnable with observability (Phase 2.4).

        Convenience method that calls observe_runnable() under the hood.
        Must call Observability.init() first.
        """
        if not cls._initialized or cls._tracer is None:
            raise RuntimeError("Observability.init() must be called before observe_runnable()")
        from .integrations.langchain.runnable_wrapper import observe_runnable
        return observe_runnable(
            runnable,
            name=name,
            root_mode=root_mode,
            session_id=session_id,
            user_id=user_id,
            business_scene=business_scene,
        )

    @classmethod
    def _instrument_openai(cls):
        """Patch OpenAI SDK for automatic LLM span creation.

        P0-2: Uses the InstrumentManager (single instance, idempotent, reversible).
        Phase 2.5: respects block_instruments.
        """
        from .instruments import Instruments
        if cls._instrument_manager is not None:
            if cls._instrument_manager.is_blocked(Instruments.OPENAI.value):
                logger.info("OpenAI instrument is blocked — skipping")
                return
            ok = cls._instrument_manager.instrument(Instruments.OPENAI.value)
            if ok:
                cls._openai_instrumentor = cls._instrument_manager.get_instrumentor(Instruments.OPENAI.value)

    @classmethod
    def _instrument_langchain(cls):
        """Auto-instrument LangChain (Phase 2.5)."""
        from .instruments import Instruments
        if cls._instrument_manager is not None:
            if cls._instrument_manager.is_blocked(Instruments.LANGCHAIN.value):
                logger.info("LangChain instrument is blocked — skipping")
                return
            cls._instrument_manager.instrument(Instruments.LANGCHAIN.value)

    @classmethod
    def shutdown(cls):
        """Shutdown the SDK — flush reporter and reset state.

        P0-1: Stops the Reporter background thread.
        P0-2: Uninstrument using the same instrumentor instance.
        Phase 2.5: Uninstrument all via InstrumentManager.
        """
        with cls._lock:
            if not cls._initialized:
                return

            # Phase 2.5: Uninstrument all instrumentors via the manager
            if cls._instrument_manager is not None:
                try:
                    cls._instrument_manager.uninstrument_all()
                except Exception as e:
                    logger.error("Instrument uninstrument error: %s", e)

            cls._openai_instrumentor = None

            # P0-1: Stop Reporter background thread
            if cls._reporter is not None:
                try:
                    cls._reporter.stop_sync()
                except Exception as e:
                    logger.error("Reporter shutdown error: %s", e)

            cls._tracer = None
            cls._reporter = None
            cls._config = None
            cls._instrument_manager = None
            cls._initialized = False

            # Reset context var to prevent cross-test/cross-invocation leakage
            try:
                from .context import _context_var
                if _context_var.get() is not None:
                    _context_var.set(None)
            except Exception:
                pass

            # Phase 2.5: Reset association context var (P1-3)
            try:
                from .association import clear_association_properties
                clear_association_properties()
            except Exception:
                pass

            logger.info("Observability SDK shutdown complete")

    # ── Phase 2.5: TASK span ──

    @classmethod
    def task(
        cls,
        name: str,
        task_type: str = "task",
        input: Any = None,
        call_id: Optional[str] = None,
        role: Optional[str] = None,
        attributes: Optional[dict] = None,
    ):
        """Create a TASK span within the current trace (Phase 2.5).

        Requires an active trace.
        """
        if not cls._initialized or cls._tracer is None:
            raise RuntimeError("Observability.init() must be called before task()")
        return cls._tracer.task(
            name=name, task_type=task_type, input=input,
            call_id=call_id, role=role, attributes=attributes,
        )

    # ── Phase 2.5: annotate ──

    @classmethod
    def annotate(
        cls,
        span=None,
        input_data=None,
        output_data=None,
        attributes=None,
        tags=None,
        error=None,
    ):
        """Annotate the current (or explicit) span (Phase 2.5).

        Fail-open: returns False if no active span.
        """
        from .annotation import annotate as _annotate
        return _annotate(
            span=span,
            input_data=input_data,
            output_data=output_data,
            attributes=attributes,
            tags=tags,
            error=error,
            tracer=cls._tracer,
        )

    # ── Phase 2.5: Association Properties ──

    @classmethod
    def set_association_properties(cls, props: dict):
        """Set association properties for the current context.

        Returns a token for reset_association_properties().
        """
        from .association import set_association_properties as _set
        return _set(props)

    @classmethod
    def reset_association_properties(cls, token):
        """Reset association properties to their previous value."""
        from .association import reset_association_properties as _reset
        _reset(token)

    @classmethod
    def association_context(cls, **kwargs):
        """Context manager for scoped association properties."""
        from .association import association_context
        return association_context(**kwargs)

    # ── Phase 2.5: Distributed Tracing ──

    @classmethod
    def inject_carrier(cls, carrier=None) -> dict:
        """Inject trace context + association into a carrier dict."""
        from .distributed import inject_carrier as _inject
        return _inject(carrier)

    @classmethod
    def extract_carrier(cls, carrier):
        """Extract trace context + association from a carrier."""
        from .distributed import extract_carrier as _extract
        return _extract(carrier)

    @classmethod
    def track_task_client_call(cls, name: str, carrier=None):
        """Context manager for a TASK client_call span."""
        if not cls._initialized or cls._tracer is None:
            raise RuntimeError("Observability.init() must be called before track_task_client_call()")
        from .distributed import ClientCallContextManager
        return ClientCallContextManager(cls._tracer, name, carrier)

    @classmethod
    def track_agent_server_call(cls, name: str, carrier=None):
        """Context manager for an AGENT server_call span."""
        if not cls._initialized or cls._tracer is None:
            raise RuntimeError("Observability.init() must be called before track_agent_server_call()")
        from .distributed import ServerCallContextManager
        return ServerCallContextManager(cls._tracer, name, carrier)

    @classmethod
    def _atexit_handler(cls):
        """Best-effort flush on process exit."""
        if cls._initialized and cls._reporter is not None:
            try:
                cls._reporter.stop_sync()
            except Exception:
                pass


def _bind_decorator_args(func, args, kwargs) -> dict:
    """Bind function arguments to a dict, skipping self/cls.

    P0-1: Used by the lazy-init instrument_tool decorator in Observability.
    """
    import inspect as _inspect
    try:
        sig = _inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()

        result = {}
        for param_name, value in bound.arguments.items():
            if param_name in ("self", "cls"):
                continue
            result[param_name] = value
        return result
    except Exception:
        return {"args": list(args), "kwargs": dict(kwargs)}