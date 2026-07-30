"""Phase 2.5 — Event attribute size uses runtime config (P1-3).

Verifies that ToolHandle.add_event and TaskHandle.add_event respect
tracer.config.max_attribute_bytes instead of the compile-time constant.
Also verifies TaskHandle.add_event normalizes the event name.
"""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest

from llm_observability import Observability


def _clean_init(**kwargs):
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(
        app_name="t", endpoint="http://localhost:99999",
        auto_instrument_openai=False, **kwargs,
    )
    return Observability._tracer


def _records():
    return list(Observability._tracer.reporter._queue)


# ── P1-3: ToolHandle.add_event respects runtime max_attribute_bytes ──

def test_tool_event_attr_respects_custom_max_attribute_bytes():
    """ToolHandle.add_event truncates event attributes to runtime config."""
    _clean_init(max_attribute_bytes=1024)
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.tool(name="t") as h:
            h.add_event("my-event", attributes={"big": "v" * 5000})
    recs = _records()
    Observability.shutdown()
    tool_recs = [r for r in recs if r["span_kind"] == "TOOL"]
    events = tool_recs[0].get("events", [])
    assert len(events) >= 1
    evt = events[-1]
    # The serialized event attributes should be within the 1024-byte budget + overhead
    attrs_str = json.dumps(evt.get("attributes", {}), ensure_ascii=False)
    assert len(attrs_str.encode("utf-8")) <= 1100, f"event attrs too large: {len(attrs_str)}"


def test_tool_event_attr_uses_runtime_not_constant():
    """Verify that a small max_attribute_bytes truncates event attrs that
    would fit under the old 16 KiB constant."""
    _clean_init(max_attribute_bytes=1024)
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.tool(name="t") as h:
            # 2000 bytes — fits under 16 KiB constant but exceeds 1024 config
            h.add_event("evt", attributes={"data": "x" * 2000})
    recs = _records()
    Observability.shutdown()
    tool_recs = [r for r in recs if r["span_kind"] == "TOOL"]
    events = tool_recs[0].get("events", [])
    assert len(events) >= 1
    evt = events[-1]
    attrs = evt.get("attributes", {})
    # Should be truncated (not the full 2000-char string)
    if isinstance(attrs, dict) and attrs.get("_truncated"):
        pass  # truncated at the dict level
    else:
        val = attrs.get("data", "")
        assert len(str(val)) < 2000, "event attr not truncated by runtime config"


# ── P1-3: TaskHandle.add_event respects runtime max_attribute_bytes ──

def test_task_event_attr_respects_custom_max_attribute_bytes():
    """TaskHandle.add_event truncates event attributes to runtime config."""
    _clean_init(max_attribute_bytes=1024)
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.task(name="sub") as h:
            h.add_event("my-event", attributes={"big": "v" * 5000})
    recs = _records()
    Observability.shutdown()
    task_recs = [r for r in recs if r["span_kind"] == "TASK"]
    events = task_recs[0].get("events", [])
    assert len(events) >= 1
    evt = events[-1]
    attrs_str = json.dumps(evt.get("attributes", {}), ensure_ascii=False)
    assert len(attrs_str.encode("utf-8")) <= 1100, f"event attrs too large: {len(attrs_str)}"


def test_task_event_attr_uses_runtime_not_constant():
    """TaskHandle.add_event uses runtime config, not a hardcoded constant."""
    _clean_init(max_attribute_bytes=1024)
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.task(name="sub") as h:
            h.add_event("evt", attributes={"data": "x" * 2000})
    recs = _records()
    Observability.shutdown()
    task_recs = [r for r in recs if r["span_kind"] == "TASK"]
    events = task_recs[0].get("events", [])
    assert len(events) >= 1
    evt = events[-1]
    attrs = evt.get("attributes", {})
    if isinstance(attrs, dict) and attrs.get("_truncated"):
        pass  # truncated at the dict level
    else:
        val = attrs.get("data", "")
        assert len(str(val)) < 2000, "event attr not truncated by runtime config"


# ── P1-3: TaskHandle.add_event normalizes event name ──

def test_task_event_name_normalized():
    """TaskHandle.add_event normalizes non-string / empty event names."""
    _clean_init()
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.task(name="sub") as h:
            h.add_event(123, attributes={"k": "v"})
            h.add_event(None, attributes={"k2": "v2"})
    recs = _records()
    Observability.shutdown()
    task_recs = [r for r in recs if r["span_kind"] == "TASK"]
    events = task_recs[0].get("events", [])
    names = [e.get("name") for e in events]
    assert "123" in names, f"numeric name not normalized: {names}"
    assert "<empty-event-name>" in names, f"None name not normalized: {names}"


def test_task_event_name_truncated():
    """TaskHandle.add_event truncates long event names."""
    _clean_init()
    tracer = Observability._tracer
    long_name = "x" * 500
    with tracer.trace(name="root"):
        with tracer.task(name="sub") as h:
            h.add_event(long_name)
    recs = _records()
    Observability.shutdown()
    task_recs = [r for r in recs if r["span_kind"] == "TASK"]
    events = task_recs[0].get("events", [])
    assert len(events) >= 1
    evt = events[-1]
    assert len(evt.get("name", "")) <= 128, f"event name not truncated: {len(evt.get('name', ''))}"
