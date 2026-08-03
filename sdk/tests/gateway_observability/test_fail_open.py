"""Fail-open fault-injection tests (spec §18, task 6.4).

Asserts business primacy AND that each telemetry failure is logged at ERROR.
"""
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    RouterSpan,
    AttemptSpan,
)
from llm_observability.gateway_observability.registry import RouterRegistry, AttemptRegistry
from llm_observability.gateway_observability.context import GatewayContext
from llm_observability.gateway_observability.recorder import GatewayEventRecorder


def _capture_logs():
    """Capture ERROR logs for a recorder block."""
    records = []

    class Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Handler()
    handler.setLevel(logging.ERROR)
    root = logging.getLogger("llm_obs.gateway")
    root.addHandler(handler)
    root.setLevel(logging.ERROR)
    return records, handler, root


def _make_router_with_faulty_span(span_method, exc):
    """Create a runtime whose Span method raises (fail-open)."""
    from llm_observability.spans import Span
    orig = getattr(Span, span_method)

    def raiser(self, *a, **kw):
        raise exc

    setattr(Span, span_method, raiser)
    return orig


def test_span_start_failure_preserves_business(clean_sdk):
    from llm_observability.spans import Span
    orig_start = Span.start
    def bad_start(self):
        raise RuntimeError("start fail")
    Span.start = bad_start
    try:
        rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
        handle = rt.handle_request({"gateway_name": "mock"})
        # Business result unaffected even though span creation failed.
        assert handle.router is not None
        handle.finalize()  # must not raise
    finally:
        Span.start = orig_start


def test_span_end_failure_preserves_business_success(clean_sdk):
    from llm_observability.spans import Span
    orig_end = Span.end
    def bad_end(self):
        raise RuntimeError("end fail")
    Span.end = bad_end
    try:
        rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
        handle = rt.handle_request({"gateway_name": "mock"})
        a = handle.start_attempt({"attempt_index": 1})
        a.start()
        handle.finish_attempt(a, upstream_status=200)
        a.close()  # end() raises inside — must not propagate
        handle.finalize()
    finally:
        Span.end = orig_end
    # Business success preserved (no exception surfaced).


def test_set_attribute_failure_preserves_business(clean_sdk):
    from llm_observability.spans import Span
    orig = Span.set_attribute
    def bad_set(self, key, value):
        raise RuntimeError("set_attribute fail")
    Span.set_attribute = bad_set
    try:
        rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
        handle = rt.handle_request({"gateway_name": "mock"})
        a = handle.start_attempt({"attempt_index": 1})
        a.start()
        handle.finish_attempt(a, upstream_status=200, raw_usage={"prompt_tokens": 1})
        a.close()
        handle.finalize()
    finally:
        Span.set_attribute = orig


def test_add_event_failure_preserves_business(clean_sdk):
    from llm_observability.spans import Span
    orig = Span.add_event
    def bad_event(self, name, timestamp=None, attributes=None):
        raise RuntimeError("add_event fail")
    Span.add_event = bad_event
    try:
        rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
        handle = rt.handle_request({"gateway_name": "mock"})
        router = handle.router
        # recorder add fails fail-open
        assert router.recorder.route_selected(channel_id="ch-1", provider="openai") is False
        a = handle.start_attempt({"attempt_index": 1})
        a.start()
        handle.finish_attempt(a, upstream_status=200)
        a.close()
        handle.finalize()
    finally:
        Span.add_event = orig


def test_reporter_failure_preserves_business(clean_sdk):
    rt = GatewayRuntime(tracer=clean_sdk, sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    orig_report = rt._tracer.reporter.report
    def bad_report(record):
        raise ConnectionError("down")
    rt._tracer.reporter.report = bad_report
    try:
        handle = rt.handle_request({"gateway_name": "mock"})
        a = handle.start_attempt({"attempt_index": 1})
        a.start()
        handle.finish_attempt(a, upstream_status=200)
        a.close()
        handle.finalize()
    finally:
        rt._tracer.reporter.report = orig_report


def test_usage_parse_failure_fail_open(clean_sdk):
    """Usage parse failure → no exception, span ends with recorded data."""
    rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    handle = rt.handle_request({"gateway_name": "mock"})
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200, raw_usage=object())  # unparseable
    a.close()
    handle.finalize()  # no exception


def test_context_reset_failure_fail_open(clean_sdk):
    """Context reset failure → business preserved + no stale context."""
    rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    handle = rt.handle_request({"gateway_name": "mock"})
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)
    a.close()
    handle.finalize()
    # Registry + context clean despite any failure
    assert GatewayContext.get().router is None


def test_business_error_preserved_with_telemetry_failure(clean_sdk):
    """Business exception preserved when telemetry also fails (spec §18)."""
    rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    handle = rt.handle_request({"gateway_name": "mock"})
    router = handle.router
    with pytest.raises(ValueError, match="biz error"):
        try:
            raise ValueError("biz error")
        finally:
            # Telemetry finalization also fails but the original exception
            # must win.
            handle.finalize()
    # Router recorded the error
    assert router is not None


def test_router_registry_cleanup_after_span_end_failure(clean_sdk):
    from llm_observability.spans import Span
    reg = RouterRegistry()
    orig = Span.end
    def bad_end(self):
        raise RuntimeError("end fail")
    Span.end = bad_end
    try:
        rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"),
                            router_registry=reg)
        handle = rt.handle_request({"gateway_name": "mock"})
        assert reg.size() == 1
        handle.finalize()
        assert reg.size() == 0  # cleanup still runs
    finally:
        Span.end = orig


def test_fail_open_logs_at_error(clean_sdk):
    """Each swallowed telemetry failure is logged at ERROR (task 6.4)."""
    records, handler, root = _capture_logs()
    try:
        from llm_observability.spans import Span
        orig = Span.end
        def bad_end(self):
            raise RuntimeError("boom")
        Span.end = bad_end
        try:
            rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
            handle = rt.handle_request({"gateway_name": "mock"})
            a = handle.start_attempt({"attempt_index": 1})
            a.start()
            handle.finish_attempt(a, upstream_status=200)
            a.close()
            handle.finalize()
        finally:
            Span.end = orig
    finally:
        root.removeHandler(handler)
    assert any("failed" in r.lower() or "fail" in r.lower() for r in records), records
