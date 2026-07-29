"""Callback Run Registry — maps LangChain run_id to observability state.

Thread-safe via threading.RLock. run_id is for callback lifecycle only;
never mapped to TraceID or used directly as parent_span_id.
"""
import logging
import threading
from dataclasses import dataclass
from typing import Optional, Any

from ...context import SpanContext

logger = logging.getLogger("llm_obs.integrations.langchain.callback_registry")


@dataclass
class CallbackRunState:
    """State for a single LangChain callback run.

    Attributes:
        run_id: LangChain run UUID (string).
        parent_run_id: Parent run UUID, None for root.
        run_type: 'chain', 'llm', 'chat_model', 'tool', 'retriever', 'custom'.
        name: Run name from config.
        context: SpanContext if this run created a real span, else nearest real parent.
        span: Span object if real, None if virtual.
        token: ContextVar token if context_owner, else None.
        context_owner: True if this run set the ContextVar.
        virtual: True for chain/prompt/parser runs (no span).
        sampled: Inherited from parent.
        first_token_seen: For TTFT tracking.
        started_at: time.time() when run started.
        ended: True after end callback processed.
    """
    run_id: str
    parent_run_id: Optional[str]
    run_type: str
    name: str
    context: Optional[SpanContext]
    span: Optional[Any]
    token: Optional[Any]
    context_owner: bool
    virtual: bool
    sampled: bool
    first_token_seen: bool
    started_at: float
    ended: bool
    previous_context: Optional[SpanContext] = None


class CallbackRunRegistry:
    """Thread-safe registry of callback run states.

    Uses threading.RLock for concurrent access. Business calls are
    NEVER executed inside the lock — only dict operations.
    """

    def __init__(self):
        self._runs: dict[str, CallbackRunState] = {}
        self._lock = threading.RLock()

    def register(self, state: CallbackRunState):
        """Register a new run state."""
        with self._lock:
            self._runs[state.run_id] = state

    def get(self, run_id: str) -> Optional[CallbackRunState]:
        """Get a run state by run_id."""
        with self._lock:
            return self._runs.get(run_id)

    def remove(self, run_id: str):
        """Remove a run state."""
        with self._lock:
            self._runs.pop(run_id, None)

    def clear(self):
        """Clear all run states."""
        with self._lock:
            self._runs.clear()

    def find_parent_context(
        self,
        run_id: str,
        current_context: Optional[SpanContext],
    ) -> Optional[SpanContext]:
        """Find the parent SpanContext for a run.

        Order:
        1. Walk parent_run_id chain → first non-None context in registry
        2. current ContextVar
        3. None (no-op)

        The run_id itself is used as starting point — we look at its
        parent_run_id chain to find a real parent context.
        """
        with self._lock:
            visited = set()
            # First, check if the run_id itself has a context
            state = self._runs.get(run_id)
            if state and state.context is not None:
                return state.context
            # Walk parent chain
            rid = state.parent_run_id if state else run_id
            while rid and rid not in visited:
                visited.add(rid)
                parent_state = self._runs.get(rid)
                if parent_state is None:
                    break
                if parent_state.context is not None:
                    return parent_state.context
                rid = parent_state.parent_run_id

        if current_context is not None:
            return current_context

        return None

    def all_runs(self) -> list[CallbackRunState]:
        """Get a snapshot of all run states (for cleanup)."""
        with self._lock:
            return list(self._runs.values())
