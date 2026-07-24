"""LLM Observability Application SDK.

Public API:
    Observability.init(...)   — Initialize SDK
    Observability.trace(...)  — Create a business task Trace
    Observability.tool(...)   — Create a Tool span (Phase 2.2)
"""
import logging
from typing import Optional

from .config import Config
from .reporter import Reporter
from .tracer import Tracer, TraceContextManager

__version__ = "0.1.0"

logger = logging.getLogger("llm_obs")


class Observability:
    """Public SDK entry point.

    Usage:
        Observability.init(app_name="my-app", endpoint="http://localhost:8001")
        with Observability.trace(name="my-task"):
            # ... business code ...
    """

    _tracer: Optional[Tracer] = None
    _reporter: Optional[Reporter] = None
    _config: Optional[Config] = None
    _initialized: bool = False

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

        Args:
            app_name: Name of the application.
            endpoint: Observability Core URL.
            api_key: Optional API key for auth.
            payload_strategy: off/metadata_only/masked/full.
            sample_rate: Sampling rate (0.0 to 1.0).
            auto_instrument_openai: Auto-patch OpenAI SDK.
        """
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
        cls._reporter = Reporter(endpoint=endpoint)
        cls._tracer = Tracer(config=cls._config, reporter=cls._reporter)

        if auto_instrument_openai:
            cls._instrument_openai()

        cls._initialized = True
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
        return cls._tracer.trace(
            name=name,
            session_id=session_id,
            user_id=user_id,
            business_scene=business_scene,
        )

    @classmethod
    def _instrument_openai(cls):
        """Patch OpenAI SDK for automatic LLM span creation."""
        try:
            from .instrumentation.openai import OpenAIInstrumentor
            OpenAIInstrumentor().instrument(tracer=cls._tracer)
        except ImportError:
            logger.warning("openai package not installed — skipping auto-instrumentation")
        except Exception as e:
            logger.error("Failed to instrument OpenAI: %s", e)

    @classmethod
    def shutdown(cls):
        """Shutdown the SDK — flush reporter and reset state."""
        if cls._reporter:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(cls._reporter.stop())
                else:
                    loop.run_until_complete(cls._reporter.stop())
            except Exception as e:
                logger.error("Reporter shutdown error: %s", e)

        # Uninstrument OpenAI if it was patched
        try:
            from .instrumentation.openai import OpenAIInstrumentor
            OpenAIInstrumentor().uninstrument()
        except Exception:
            pass

        cls._tracer = None
        cls._reporter = None
        cls._config = None
        cls._initialized = False
