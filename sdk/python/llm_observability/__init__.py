"""LLM Observability Application SDK.

Public API:
    Observability.init(...)   — Initialize SDK
    Observability.trace(...)  — Create a business task Trace
    Observability.tool(...)   — Create a Tool span (Phase 2.2)

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
        capture_retriever_content: bool = False,
    ):
        """Initialize the SDK. Idempotent — safe to call multiple times.

        P0-1: Auto-starts the Reporter in a background thread.
        P0-2: Creates and holds a single OpenAIInstrumentor instance.

        Args:
            app_name: Name of the application.
            endpoint: Observability Core URL.
            api_key: Optional API key for auth.
            payload_strategy: off/metadata_only/masked/full.
            sample_rate: Sampling rate (0.0 to 1.0).
            auto_instrument_openai: Auto-patch OpenAI SDK.
        """
        with cls._lock:
            if cls._initialized:
                logger.warning("Observability.init() already called — skipping (idempotent)")
                return

            cls._config = Config(
                app_name=app_name,
                endpoint=endpoint,
                api_key=api_key,
                payload_strategy=payload_strategy,
                sample_rate=sample_rate,
                auto_instrument_openai=auto_instrument_openai,
                capture_retriever_content=capture_retriever_content,
            )

            # P0-1: Reporter with api_key for auth (P1-2)
            cls._reporter = Reporter(endpoint=endpoint, api_key=api_key)
            cls._tracer = Tracer(config=cls._config, reporter=cls._reporter)

            # P0-1: Auto-start Reporter in background thread
            cls._reporter.start_sync()

            # P0-2: Use a single instrumentor instance
            if auto_instrument_openai:
                cls._instrument_openai()

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

        P0-2: Uses a single instrumentor instance held as a class attribute.
        """
        try:
            from .instrumentation.openai import OpenAIInstrumentor
            cls._openai_instrumentor = OpenAIInstrumentor()
            cls._openai_instrumentor.instrument(tracer=cls._tracer)
        except ImportError:
            logger.warning("openai package not installed — skipping auto-instrumentation")
        except Exception as e:
            logger.error("Failed to instrument OpenAI: %s", e)

    @classmethod
    def shutdown(cls):
        """Shutdown the SDK — flush reporter and reset state.

        P0-1: Stops the Reporter background thread.
        P0-2: Uninstrument using the same instrumentor instance.
        """
        with cls._lock:
            if not cls._initialized:
                return

            # P0-2: Uninstrument using the SAME instance
            if cls._openai_instrumentor is not None:
                try:
                    cls._openai_instrumentor.uninstrument()
                except Exception as e:
                    logger.error("OpenAI uninstrument error: %s", e)
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
            cls._initialized = False

            # Reset context var to prevent cross-test/cross-invocation leakage
            try:
                from .context import _context_var
                if _context_var.get() is not None:
                    _context_var.set(None)
            except Exception:
                pass

            logger.info("Observability SDK shutdown complete")

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