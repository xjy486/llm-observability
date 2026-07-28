"""Tracer — manages span lifecycle and trace context.

Provides the Observability.trace() context manager that creates an
AGENT root span (spec §10, §11).

P1-2: Implements head sampling at root trace creation.
"""
import logging
import random
from typing import Optional, Any

from .config import Config
from .context import SpanContext, get_current_context, set_context, reset_context
from .spans import Span, SpanKind
from .reporter import Reporter
from .utils.ids import generate_trace_id, generate_span_id

logger = logging.getLogger("llm_obs.tracer")


class TraceContextManager:
    """Context manager for a trace / AGENT span.

    On enter: creates trace_id, root span_id, sets ContextVar.
    On exit: records end_time, sets status, enqueues to reporter,
             restores parent context.
    On exception: sets ERROR status, re-raises.

    P1-2: Respects sample_rate — if not sampled, span is still created
          (for context propagation) but not reported to the backend.
    """

    def __init__(
        self,
        tracer: "Tracer",
        name: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        business_scene: Optional[str] = None,
    ):
        self._tracer = tracer
        self._name = name
        self._session_id = session_id
        self._user_id = user_id
        self._business_scene = business_scene
        self._span: Optional[Span] = None
        self._token = None
        self._sampled = True

    def __enter__(self):
        trace_id = generate_trace_id()
        span_id = generate_span_id()
        parent = get_current_context()

        # P1-2: Head sampling at root trace creation
        self._sampled = random.random() < self._tracer.config.sample_rate

        ctx = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent.span_id if parent else None,
            span_kind=SpanKind.AGENT,
            sampled=self._sampled,
        )
        self._token = set_context(ctx)

        self._span = Span(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=ctx.parent_span_id,
            span_name="agent.run" if not parent else f"agent.{self._name}",
            span_kind=SpanKind.AGENT,
            session_id=self._session_id,
            user_id=self._user_id,
            app_name=self._tracer.config.app_name,
            business_scene=self._business_scene,
        )
        self._span.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # P1-5: GeneratorExit and CancelledError are control flow from
        # stream/astream close, not business errors.
        import asyncio as _asyncio
        is_control_flow = (
            exc_type is GeneratorExit
            or (hasattr(_asyncio, 'CancelledError') and exc_type is _asyncio.CancelledError)
        )

        if exc_type is not None and not is_control_flow:
            self._span.set_error(
                error_type=exc_type.__name__,
                error_message=str(exc_val),
            )
        else:
            self._span.set_status("OK")

        self._span.end()

        # P1-2: Only report if sampled
        if self._sampled:
            try:
                self._tracer.reporter.report(self._span.to_record())
            except Exception as e:
                logger.error("Failed to report span: %s", e)

        reset_context(self._token)
        return False  # do not suppress exceptions


class Tracer:
    """Manages trace creation and span reporting.

    Holds the Config and Reporter. Provides trace() context manager.
    """

    def __init__(self, config: Config, reporter: Reporter):
        self.config = config
        self.reporter = reporter

    def trace(
        self,
        name: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        business_scene: Optional[str] = None,
    ) -> TraceContextManager:
        """Create a new trace with an AGENT root span.

        Args:
            name: Task name for the trace (e.g. 'fix_login_task').
            session_id: Optional session ID for grouping.
            user_id: Optional user ID.
            business_scene: Optional business scene tag.

        Returns:
            A context manager that tracks the AGENT span lifecycle.
        """
        return TraceContextManager(
            tracer=self,
            name=name,
            session_id=session_id,
            user_id=user_id,
            business_scene=business_scene,
        )

    def tool(
        self,
        name: str,
        tool_type: Optional[str] = None,
        input: Any = None,
        call_id: Optional[str] = None,
        attributes: Optional[dict] = None,
    ):
        """Create a TOOL span context manager (Phase 2.2).

        Requires an active trace (AGENT context). Raises RuntimeError otherwise.
        """
        from .tool import ToolContextManager
        return ToolContextManager(
            tracer=self,
            name=name,
            tool_type=tool_type,
            input=input,
            call_id=call_id,
            attributes=attributes,
        )

    def instrument_tool(self, name: str, tool_type: Optional[str] = None):
        """Create a tool decorator that wraps a function with a TOOL span (Phase 2.2)."""
        from .tool import instrument_tool as _instrument_tool
        return _instrument_tool(self, name=name, tool_type=tool_type)
