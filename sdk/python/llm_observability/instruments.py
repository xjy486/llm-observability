"""Instruments — Phase 2.5.

Defines the Instruments enum and the InstrumentManager that holds real
Instrumentor instances and manages their lifecycle (idempotent, thread-safe,
reversible).

Rules (spec §4):
    block_instruments takes priority over auto_instrument_*
    instrument()/uninstrument() must be idempotent, thread-safe, reversible
    repeated init/shutdown must not re-patch
    a single Instrument failure must not affect business or other Instruments
    Observability must hold real Instrumentor instances
"""
import logging
import threading
from enum import Enum
from typing import Optional

logger = logging.getLogger("llm_obs.instruments")


class Instruments(str, Enum):
    """Identifiers for auto-instrumentation modules."""
    OPENAI = "openai"
    LANGCHAIN = "langchain"


class InstrumentManager:
    """Manages the lifecycle of all auto-instrumentors.

    Holds real Instrumentor instances. instrument()/uninstrument() are
    idempotent and thread-safe. A single instrument failure is isolated.
    """

    def __init__(self, tracer):
        self._tracer = tracer
        self._lock = threading.Lock()
        self._instrumentors: dict[str, object] = {}
        self._blocked: set[str] = set()

    def set_blocked(self, blocked: Optional[set]) -> None:
        """Set the set of blocked instrument names."""
        with self._lock:
            self._blocked = set(blocked) if blocked else set()

    def is_blocked(self, name: str) -> bool:
        with self._lock:
            return name in self._blocked

    def instrument(self, name: str, force: bool = False) -> bool:
        """Instrument a module by name. Idempotent.

        Returns True if the instrumentor is now active (or was already active).
        Returns False if blocked or the module failed to instrument.
        """
        with self._lock:
            if name in self._instrumentors:
                return True  # already instrumented (idempotent)
            if not force and name in self._blocked:
                logger.info("Instrument '%s' is blocked — skipping", name)
                return False

        try:
            instrumentor = self._create_instrumentor(name)
            if instrumentor is None:
                return False
            instrumentor.instrument(tracer=self._tracer)
            with self._lock:
                # Another thread may have instrumented in the meantime; if so,
                # uninstrument our duplicate to stay idempotent.
                if name in self._instrumentors:
                    try:
                        instrumentor.uninstrument()
                    except Exception:
                        pass
                    return True
                self._instrumentors[name] = instrumentor
            logger.info("Instrument '%s' installed", name)
            return True
        except ImportError:
            logger.warning("Instrument '%s' dependency not installed — skipping", name)
            return False
        except Exception as e:
            # Single instrument failure must not affect others
            logger.error("Instrument '%s' failed: %s", name, e)
            return False

    def uninstrument(self, name: str) -> bool:
        """Uninstrument a module by name. Idempotent."""
        with self._lock:
            instrumentor = self._instrumentors.pop(name, None)
        if instrumentor is None:
            return False
        try:
            instrumentor.uninstrument()
            logger.info("Instrument '%s' removed", name)
            return True
        except Exception as e:
            logger.error("Uninstrument '%s' failed: %s", name, e)
            return False

    def uninstrument_all(self) -> None:
        """Uninstrument all active instrumentors."""
        with self._lock:
            names = list(self._instrumentors.keys())
        for name in names:
            self.uninstrument(name)

    def get_instrumentor(self, name: str):
        with self._lock:
            return self._instrumentors.get(name)

    @property
    def active(self) -> list[str]:
        with self._lock:
            return list(self._instrumentors.keys())

    def _create_instrumentor(self, name: str):
        """Create a fresh instrumentor instance for the given name."""
        if name == Instruments.OPENAI.value:
            from .instrumentation.openai import OpenAIInstrumentor
            return OpenAIInstrumentor()
        if name == Instruments.LANGCHAIN.value:
            from .instrumentation.langchain import LangChainInstrumentor
            return LangChainInstrumentor()
        logger.warning("Unknown instrument: %s", name)
        return None
