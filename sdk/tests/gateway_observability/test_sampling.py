"""Sampling inheritance tests (spec §17)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    sampling_from_traceparent,
)
from llm_observability.gateway_observability.registry import RouterRegistry, AttemptRegistry


def test_trace_flags_01_samples():
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert sampling_from_traceparent(tp) is True


def test_trace_flags_00_does_not_report():
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
    assert sampling_from_traceparent(tp) is False


def test_no_upstream_returns_none():
    assert sampling_from_traceparent(None) is None
    assert sampling_from_traceparent("") is None
    assert sampling_from_traceparent("garbage") is None


def test_sampled_out_honored_no_records(clean_sdk):
    """Upstream trace_flags=00 → no Reporter Record, business still runs."""
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
    rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    handle = rt.handle_request({"gateway_name": "mock"}, upstream_traceparent=tp)
    router = handle.router
    # Business path still works
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)
    a.close()
    handle.finalize()

    assert router.span is not None
    # Sampled-out: spans exist but nothing was reported to the reporter.
    assert len(clean_sdk.reporter._queue) == 0


def test_no_upstream_local_sample_rate(clean_sdk):
    """No upstream trace + sample_rate=1.0 → Root Router reported."""
    rt = GatewayRuntime(tracer=clean_sdk, sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    handle = rt.handle_request({"gateway_name": "mock"})
    router = handle.router
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)
    a.close()
    handle.finalize()
    assert router.span.parent_span_id is None
    assert len(clean_sdk.reporter._queue) >= 1


def test_sampled_out_still_propagates_traceparent(clean_sdk):
    """Sampled-out still propagates the upstream traceparent downstream."""
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
    rt = GatewayRuntime(sample_rate=1.0, privacy=PrivacyGuard(secret="s"))
    handle = rt.handle_request({"gateway_name": "mock"}, upstream_traceparent=tp)
    router = handle.router
    # The Router trace_id is inherited from the upstream traceparent.
    assert router.span.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    handle.finalize()


def test_sampling_never_overridden_by_resample(clean_sdk):
    """Gateway never re-randomizes an upstream sampling decision."""
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    rt = GatewayRuntime(sample_rate=0.0, privacy=PrivacyGuard(secret="s"))
    handle = rt.handle_request({"gateway_name": "mock"}, upstream_traceparent=tp)
    # Even with local sample_rate=0.0, upstream 01 wins.
    assert handle.sampled is True
    handle.finalize()
