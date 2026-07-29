"""Distributed Tracing Helpers — Phase 2.5.

Provides carrier injection/extraction and Client/Server span helpers.

API (spec §10):
    inject_carrier(carrier=None) -> dict
    extract_carrier(carrier) -> Optional[ExtractedContext]
    track_task_client_call(name, carrier=None) -> context manager
    track_agent_server_call(name, carrier=None) -> context manager

Default propagation headers: traceparent, baggage
Compat headers (read-only): X-Session-Id, X-User-Id, X-App-Name, X-Business-Scene

Carrier only carries Trace Context + Association metadata — never Prompt,
Response, API Key, or full Tool Output.

P1-5: inject_carrier mutates the provided carrier IN PLACE and returns the
same object (``returned is carrier``). Baggage values are W3C percent-encoded
(handles comma/equals/space/Unicode/control chars).
"""
import logging
from typing import Any, Optional, Union
from urllib.parse import quote, unquote

from .context import (
    SpanContext, get_current_context, set_context, reset_context,
)
from .spans import Span, SpanKind
from .propagation import inject_traceparent, extract_traceparent, TRACEPARENT_RE
from .utils.ids import generate_trace_id, generate_span_id
from .task import TaskContextManager
from .association import (
    get_association_properties, set_association_properties,
    reset_association_properties, _sanitize_value, ALIASES,
)

logger = logging.getLogger("llm_obs.distributed")


def _encode_baggage_value(value: str) -> str:
    """W3C baggage percent-encoding for a value.

    Encodes commas, equals, spaces, and non-token characters so the baggage
    header remains parseable. Control characters are also encoded.
    """
    if value is None:
        return ""
    # quote with safe='' encodes everything except alphanumerics and _.-~
    # We also encode ',' and '=' (reserved baggage delimiters).
    return quote(str(value), safe="")


def inject_carrier(carrier: Optional[dict] = None) -> dict:
    """Inject trace context + association metadata into a carrier dict.

    P1-5: Mutates the provided carrier IN PLACE and returns the SAME object
    (``returned is carrier``). If carrier is None, a new dict is created.
    Only trace context and association metadata are included — never payload
    or secrets. Baggage values are W3C percent-encoded.
    """
    if carrier is None:
        carrier = {}
    # P1-5: mutate in place (do NOT copy)

    ctx = get_current_context()
    if ctx is not None:
        carrier["traceparent"] = inject_traceparent(ctx)

    assoc = get_association_properties()

    # Fall back to the active span's explicit values if association ContextVar
    # is empty (supports trace(session_id=...) set explicitly on the span).
    active_span = None
    if ctx is not None:
        try:
            from .span_registry import get_span_event_sink
            active_span_sink = get_span_event_sink(ctx.trace_id, ctx.span_id)
            if active_span_sink is not None:
                active_span = active_span_sink._span
        except Exception:
            pass

    user = assoc.user
    session_id = assoc.session_id
    business_scenario = assoc.business_scenario
    message_id = assoc.message_id
    app_name = None

    if active_span is not None:
        if user is None and getattr(active_span, "user_id", None) is not None:
            user = active_span.user_id
        if session_id is None and getattr(active_span, "session_id", None) is not None:
            session_id = active_span.session_id
        if business_scenario is None and getattr(active_span, "business_scene", None) is not None:
            business_scenario = active_span.business_scene
        if message_id is None and getattr(active_span, "message_id", None) is not None:
            message_id = active_span.message_id
        if getattr(active_span, "app_name", None) is not None:
            app_name = active_span.app_name

    # Compat headers (plain, unencoded — HTTP header values)
    if user is not None:
        carrier["X-User-Id"] = user
    if session_id is not None:
        carrier["X-Session-Id"] = session_id
    if business_scenario is not None:
        carrier["X-Business-Scene"] = business_scenario
    if app_name is not None:
        carrier["X-App-Name"] = app_name

    # W3C baggage (percent-encoded values)
    baggage_parts = []
    if user is not None:
        baggage_parts.append(f"user={_encode_baggage_value(user)}")
    if session_id is not None:
        baggage_parts.append(f"session_id={_encode_baggage_value(session_id)}")
    if business_scenario is not None:
        baggage_parts.append(f"business_scenario={_encode_baggage_value(business_scenario)}")
    if message_id is not None:
        baggage_parts.append(f"message_id={_encode_baggage_value(message_id)}")
    if app_name is not None:
        baggage_parts.append(f"app_name={_encode_baggage_value(app_name)}")

    if baggage_parts:
        carrier["baggage"] = ",".join(baggage_parts)
    elif "baggage" in carrier:
        del carrier["baggage"]

    return carrier


class ExtractedContext:
    """Result of carrier extraction (carrier -> trace + association)."""

    def __init__(
        self,
        trace_id: str,
        parent_span_id: str,
        trace_flags: str,
        association: Optional[dict] = None,
        inherited: bool = True,
    ):
        self.trace_id = trace_id
        self.parent_span_id = parent_span_id
        self.trace_flags = trace_flags
        self.association = association or {}
        self.inherited = inherited

    @property
    def sampled(self) -> bool:
        try:
            return (int(self.trace_flags, 16) & 0x01) == 0x01
        except Exception:
            return True


def extract_carrier(carrier: dict) -> Optional[ExtractedContext]:
    """Extract trace context + association metadata from a carrier.

    Returns None if the carrier has no valid traceparent (illegal/missing
    context). Association metadata is parsed from baggage and compat headers.
    """
    if carrier is None:
        return None

    # Normalize carrier to a plain dict (supports httpx/requests Headers,
    # Starlette HeaderScope, mappings, etc.)
    if hasattr(carrier, "items"):
        items = dict(carrier.items())
    elif isinstance(carrier, dict):
        items = dict(carrier)
    else:
        items = dict(carrier)

    # Case-insensitive lookup helper
    lower_items = {str(k).lower(): v for k, v in items.items()}

    traceparent = (
        lower_items.get("traceparent")
        or items.get("traceparent")
    )
    if isinstance(traceparent, (list, tuple)) and traceparent:
        traceparent = traceparent[0]
    if not traceparent:
        return None

    extracted = extract_traceparent(str(traceparent))
    if extracted is None:
        return None

    # Parse association from baggage + compat headers
    assoc: dict[str, str] = {}

    baggage = lower_items.get("baggage") or items.get("baggage")
    if isinstance(baggage, (list, tuple)) and baggage:
        baggage = baggage[0]
    if baggage:
        try:
            for pair in str(baggage).split(","):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    k = k.strip()
                    v = unquote(v.strip())  # P1-5: decode percent-encoded baggage
                    canonical = ALIASES.get(k, k)
                    if canonical in ("user", "session_id", "message_id", "business_scenario", "app_name"):
                        assoc[canonical] = _sanitize_value(v)
        except Exception:
            pass

    # Compat headers (override baggage if present)
    compat_map = {
        "x-user-id": "user",
        "x-session-id": "session_id",
        "x-business-scene": "business_scenario",
        "x-app-name": "app_name",
    }
    for header, canonical in compat_map.items():
        val = lower_items.get(header)
        if isinstance(val, (list, tuple)) and val:
            val = val[0]
        if val:
            assoc[canonical] = _sanitize_value(val)

    return ExtractedContext(
        trace_id=extracted.trace_id,
        parent_span_id=extracted.parent_span_id,
        trace_flags=extracted.trace_flags,
        association=assoc,
        inherited=True,
    )


class ClientCallContextManager:
    """Context manager for a TASK client_call span.

    Creates a TASK span (task.type=client_call, task.role=client) and injects
    the trace carrier into the provided carrier dict so the downstream service
    inherits the same TraceID.

    Usage:
        headers = {}
        with track_task_client_call("profile-service", carrier=headers) as span:
            response = requests.post(url, headers=headers, json=data)
            span.set_output(response.text)
    """

    def __init__(self, tracer, name: str, carrier: Optional[dict] = None):
        self._tracer = tracer
        self._name = name
        self._carrier = carrier if carrier is not None else {}
        self._task_cm: Optional[TaskContextManager] = None
        self._handle = None
        self._assoc_token = None

    def __enter__(self):
        current = get_current_context()
        if current is None:
            raise RuntimeError(
                "track_task_client_call requires an active trace."
            )
        self._task_cm = TaskContextManager(
            tracer=self._tracer,
            name=self._name,
            task_type="client_call",
            role="client",
        )
        self._handle = self._task_cm.__enter__()

        # Inject carrier so downstream inherits the trace.
        # Mutate the caller's carrier dict IN PLACE so their headers get the
        # traceparent (inject_carrier returns a copy by design for the
        # standalone API; here we must populate the provided carrier).
        injected = inject_carrier(self._carrier)
        self._carrier.update(injected)
        return self._handle

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._task_cm.__exit__(exc_type, exc_val, exc_tb)

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__exit__(exc_type, exc_val, exc_tb)


class ServerCallContextManager:
    """Context manager for an AGENT server_call span.

    Extracts trace context from the carrier. If valid, inherits the TraceID
    and sets the Client TASK as parent. If invalid/missing, creates a new
    trace (fail-open).

    Creates an AGENT span with operation.type=server_call, span.role=server.
    Also sets association properties from the carrier for the duration of the
    call.

    Usage:
        with track_agent_server_call("profile-handler", carrier=request.headers):
            ...
    """

    def __init__(self, tracer, name: str, carrier: Optional[dict] = None):
        self._tracer = tracer
        self._name = name
        self._carrier = carrier
        self._span: Optional[Span] = None
        self._token = None
        self._assoc_token = None
        self._sampled = True
        self._created_trace = False

    def _build_context(self) -> tuple[Optional[str], Optional[str], bool, dict]:
        """Return (trace_id, parent_span_id, sampled, association_dict).

        P0-2: legal remote trace_flags → inherit sampled; no valid carrier →
        use the local sample_rate.
        """
        extracted = extract_carrier(self._carrier) if self._carrier is not None else None
        if extracted is not None:
            return extracted.trace_id, extracted.parent_span_id, extracted.sampled, extracted.association
        # No valid carrier — create a new trace, sampling per local config
        import random
        sampled = random.random() < self._tracer.config.sample_rate
        return generate_trace_id(), None, sampled, {}

    def __enter__(self):
        trace_id, parent_span_id, sampled, assoc = self._build_context()

        # Set association from carrier for the duration of the server call
        if assoc:
            self._assoc_token = set_association_properties(assoc)

        span_id = generate_span_id()
        ctx = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            span_kind=SpanKind.AGENT,
            sampled=sampled,
        )
        self._token = set_context(ctx)
        self._sampled = sampled

        from .association import get_association_properties
        assoc_props = get_association_properties()

        span = Span(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            span_name=f"agent.{self._name}",
            span_kind=SpanKind.AGENT,
            app_name=self._tracer.config.app_name,
            user_id=assoc_props.user,
            session_id=assoc_props.session_id,
            message_id=assoc_props.message_id,
            business_scene=assoc_props.business_scenario,
        )
        span.set_attribute("operation.type", "server_call")
        span.set_attribute("span.role", "server")
        span.start()

        try:
            from .span_registry import register_span_event_sink
            register_span_event_sink(span)
        except Exception:
            pass

        self._span = span
        return span

    def __exit__(self, exc_type, exc_val, exc_tb):
        is_control_flow = False
        if exc_type is not None:
            try:
                from .integrations.langchain.compat import is_control_flow_exception
                is_control_flow = is_control_flow_exception(exc_val)
            except ImportError:
                import asyncio as _asyncio
                is_control_flow = (
                    exc_type is GeneratorExit
                    or (hasattr(_asyncio, 'CancelledError') and exc_type is _asyncio.CancelledError)
                )

        try:
            try:
                if exc_type is not None and not is_control_flow:
                    self._span.set_error(
                        error_type=exc_type.__name__,
                        error_message=str(exc_val) if exc_val else "",
                    )
                else:
                    self._span.set_status("OK")
                self._span.end()
                if self._sampled:
                    self._tracer.reporter.report(self._span.to_record())
            except Exception:
                logger.exception("server_call span finalization failed")
        finally:
            try:
                if self._span is not None:
                    from .span_registry import unregister_span_event_sink
                    unregister_span_event_sink(self._span.trace_id, self._span.span_id)
            except Exception:
                pass
            if self._token is not None:
                reset_context(self._token)
            if self._assoc_token is not None:
                reset_association_properties(self._assoc_token)
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return self.__exit__(exc_type, exc_val, exc_tb)
