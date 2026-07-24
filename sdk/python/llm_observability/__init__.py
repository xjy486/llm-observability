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
from typing import Optional

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
            logger.info("Observability SDK shutdown complete")

    @classmethod
    def _atexit_handler(cls):
        """Best-effort flush on process exit."""
        if cls._initialized and cls._reporter is not None:
            try:
                cls._reporter.stop_sync()
            except Exception:
                pass
