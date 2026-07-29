"""Active span event sinks for callbacks that only have a SpanContext."""
import threading
from typing import Any, Optional


class SpanEventSink:
    """Small event-writing interface exposed to integrations."""

    def __init__(self, span: Any):
        self._span = span

    def add_event(self, name: str, timestamp: float = None, attributes: dict = None):
        return self._span.add_event(name, timestamp, attributes)

    def set_attribute(self, key: str, value: Any):
        return self._span.set_attribute(key, value)


_lock = threading.RLock()
_sinks: dict[tuple[str, str], SpanEventSink] = {}


def register_span_event_sink(span: Any) -> Optional[SpanEventSink]:
    if span is None:
        return None
    sink = SpanEventSink(span)
    with _lock:
        _sinks[(str(span.trace_id), str(span.span_id))] = sink
    return sink


def get_span_event_sink(trace_id: str, span_id: str) -> Optional[SpanEventSink]:
    with _lock:
        return _sinks.get((str(trace_id), str(span_id)))


def unregister_span_event_sink(trace_id: str, span_id: str):
    with _lock:
        _sinks.pop((str(trace_id), str(span_id)), None)


def clear_span_event_sinks():
    with _lock:
        _sinks.clear()
