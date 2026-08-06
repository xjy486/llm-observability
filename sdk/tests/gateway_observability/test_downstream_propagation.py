"""P1-6: downstream traceparent propagation tests (adversarial).

The Attempt continues the trace downstream: trace_id = Router trace_id,
parent = Attempt span_id, flags = inherited sampling decision (00/01).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import GatewayRuntime
from llm_observability.gateway_observability.context import clear_gateway_context
from llm_observability.gateway_observability.propagation import (
    inject_downstream_trace_headers,
)


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


def _parse(traceparent: str):
    version, trace_id, parent_id, flags = traceparent.split("-")
    return {"version": version, "trace_id": trace_id, "parent_id": parent_id, "flags": flags}


class TestDownstreamPropagation:
    def test_attempt_downstream_traceparent_parent_is_attempt(self, tracer):
        runtime = GatewayRuntime(tracer=tracer, sample_rate=1.0)
        handle = runtime.handle_request({})
        attempt = handle.start_attempt({})
        attempt.start()
        headers = inject_downstream_trace_headers(handle.router, attempt)
        parsed = _parse(headers["traceparent"])
        assert parsed["parent_id"] == attempt.span.span_id
        assert parsed["trace_id"] == handle.router.span.trace_id
        attempt.close()
        handle.finalize()

    def test_sampled_zero_downstream_trace_flags_00(self, tracer):
        runtime = GatewayRuntime(tracer=tracer, sample_rate=1.0)
        handle = runtime.handle_request(
            {}, upstream_traceparent="00-0af7651916cd43dd8448eb211c80319c-00f067aa0ba902b7-00",
        )
        attempt = handle.start_attempt({})
        attempt.start()
        headers = inject_downstream_trace_headers(handle.router, attempt)
        parsed = _parse(headers["traceparent"])
        assert parsed["flags"] == "00"
        attempt.close()
        handle.finalize()

    def test_sampled_one_downstream_trace_flags_01(self, tracer):
        runtime = GatewayRuntime(tracer=tracer, sample_rate=1.0)
        handle = runtime.handle_request(
            {}, upstream_traceparent="00-0af7651916cd43dd8448eb211c80319c-00f067aa0ba902b7-01",
        )
        attempt = handle.start_attempt({})
        attempt.start()
        headers = inject_downstream_trace_headers(handle.router, attempt)
        parsed = _parse(headers["traceparent"])
        assert parsed["flags"] == "01"
        attempt.close()
        handle.finalize()

    def test_remote_trace_id_preserved_downstream(self, tracer):
        upstream_tid = "0af7651916cd43dd8448eb211c80319c"
        runtime = GatewayRuntime(tracer=tracer, sample_rate=1.0)
        handle = runtime.handle_request(
            {}, upstream_traceparent=f"00-{upstream_tid}-00f067aa0ba902b7-01",
        )
        attempt = handle.start_attempt({})
        attempt.start()
        headers = inject_downstream_trace_headers(handle.router, attempt)
        parsed = _parse(headers["traceparent"])
        assert parsed["trace_id"] == upstream_tid
        attempt.close()
        handle.finalize()

    def test_local_root_trace_id_propagated_downstream(self, tracer):
        runtime = GatewayRuntime(tracer=tracer, sample_rate=1.0)
        handle = runtime.handle_request({})  # no upstream traceparent
        attempt = handle.start_attempt({})
        attempt.start()
        headers = inject_downstream_trace_headers(handle.router, attempt)
        parsed = _parse(headers["traceparent"])
        assert parsed["trace_id"] == handle.router.span.trace_id
        assert len(parsed["trace_id"]) == 32
        assert parsed["flags"] == "01"  # local sample_rate=1.0 sampled in
        attempt.close()
        handle.finalize()
