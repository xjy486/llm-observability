"""P0-1/P0-2: trace identity + trace origin adversarial tests.

Covers:
- No-SDK/no-traceparent Routers get valid, unique, non-zero TraceIDs.
- Attempts inherit the Router TraceID.
- trace_origin / upstream_trace_present are exact for sdk / remote / gateway.
- Trace metadata is consistent with the actual parent IDs.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability.attributes import ATTR_GATEWAY
from llm_observability.gateway_observability.router_span import (
    RouterSpan,
    ResolvedGatewayParent,
    ORIGIN_SDK_CONTEXT,
    ORIGIN_REMOTE_TRACEPARENT,
    ORIGIN_GATEWAY_ROOT,
)

_HEX = set("0123456789abcdef")
_ALL_ZERO = "0" * 32


def _assert_valid_trace_id(trace_id):
    assert trace_id is not None, "trace_id must never be None"
    assert isinstance(trace_id, str)
    assert len(trace_id) == 32, f"trace_id must be 32 chars, got {len(trace_id)}"
    assert set(trace_id) <= _HEX, f"trace_id must be lowercase hex: {trace_id}"
    assert trace_id != _ALL_ZERO, "all-zero trace_id is forbidden"


class TestNoSdkTraceIdentity:
    def test_no_sdk_router_generates_valid_trace_id(self, clean_sdk):
        router = RouterSpan(tracer=clean_sdk).start()
        try:
            assert router.span is not None
            _assert_valid_trace_id(router.span.trace_id)
            assert router.span.parent_span_id is None
        finally:
            router.close()

    def test_no_sdk_attempt_inherits_router_trace_id(self, clean_sdk):
        router = RouterSpan(tracer=clean_sdk).start()
        attempt = router.attempt(attempt_index=None).start()
        try:
            assert attempt.span.trace_id == router.span.trace_id
            assert attempt.span.parent_span_id == router.span.span_id
        finally:
            attempt.close()
            router.close()

    def test_no_sdk_requests_generate_distinct_trace_ids(self, clean_sdk):
        trace_ids = set()
        for _ in range(8):
            router = RouterSpan(tracer=clean_sdk).start()
            _assert_valid_trace_id(router.span.trace_id)
            trace_ids.add(router.span.trace_id)
            router.close()
        assert len(trace_ids) == 8, "consecutive root requests must get distinct trace IDs"

    def test_router_never_reports_null_trace_id(self, clean_sdk):
        # No SDK context, no upstream traceparent, and even a garbage upstream
        # trace id must all yield a non-null trace id.
        for upstream in (None, "", "not-a-trace-id", _ALL_ZERO):
            router = RouterSpan(tracer=clean_sdk, upstream_trace_id=upstream).start()
            assert router.span.trace_id is not None
            _assert_valid_trace_id(router.span.trace_id)
            router.close()

    def test_router_never_reports_all_zero_trace_id(self, clean_sdk):
        router = RouterSpan(
            tracer=clean_sdk, upstream_trace_id=_ALL_ZERO,
            upstream_parent_span_id="00f067aa0ba902b7",
        ).start()
        assert router.span.trace_id != _ALL_ZERO
        router.close()


class TestTraceOrigin:
    def test_remote_traceparent_sets_trace_origin_remote(self, clean_sdk):
        upstream_tid = "0af7651916cd43dd8448eb211c80319c"
        router = RouterSpan(
            tracer=clean_sdk,
            upstream_trace_id=upstream_tid,
            upstream_parent_span_id="00f067aa0ba902b7",
        ).start()
        try:
            assert router.span.attributes[ATTR_GATEWAY["trace_origin"]] == "remote"
            assert router.span.trace_id == upstream_tid
            assert router.span.parent_span_id == "00f067aa0ba902b7"
        finally:
            router.close()

    def test_remote_traceparent_sets_upstream_trace_present_true(self, clean_sdk):
        router = RouterSpan(
            tracer=clean_sdk,
            upstream_trace_id="0af7651916cd43dd8448eb211c80319c",
            upstream_parent_span_id="00f067aa0ba902b7",
        ).start()
        try:
            assert router.span.attributes[ATTR_GATEWAY["upstream_trace_present"]] is True
        finally:
            router.close()

    def test_local_root_sets_trace_origin_gateway(self, clean_sdk):
        router = RouterSpan(tracer=clean_sdk).start()
        try:
            assert router.span.attributes[ATTR_GATEWAY["trace_origin"]] == "gateway"
            assert router.span.attributes[ATTR_GATEWAY["upstream_trace_present"]] is False
        finally:
            router.close()

    def test_sdk_context_sets_trace_origin_sdk(self, clean_sdk):
        from llm_observability.context import SpanContext, set_context, reset_context
        from llm_observability.utils.ids import generate_trace_id, generate_span_id
        sdk_trace_id = generate_trace_id()
        sdk_span_id = generate_span_id()
        token = set_context(SpanContext(
            trace_id=sdk_trace_id, span_id=sdk_span_id,
            parent_span_id=None, span_kind="LLM",
        ))
        try:
            router = RouterSpan(tracer=clean_sdk).start()
            try:
                assert router.span.attributes[ATTR_GATEWAY["trace_origin"]] == "sdk"
                assert router.span.attributes[ATTR_GATEWAY["upstream_trace_present"]] is True
                assert router.span.trace_id == sdk_trace_id
                assert router.span.parent_span_id == sdk_span_id
            finally:
                router.close()
        finally:
            reset_context(token)

    def test_trace_metadata_consistent_with_parent_ids(self, clean_sdk):
        # gateway origin ⇒ root (no parent); sdk/remote ⇒ non-null parent.
        cases = []
        router_g = RouterSpan(tracer=clean_sdk).start()
        cases.append(router_g)
        router_r = RouterSpan(
            tracer=clean_sdk,
            upstream_trace_id="0af7651916cd43dd8448eb211c80319c",
            upstream_parent_span_id="00f067aa0ba902b7",
        ).start()
        cases.append(router_r)
        try:
            for router in cases:
                origin = router.span.attributes[ATTR_GATEWAY["trace_origin"]]
                present = router.span.attributes[ATTR_GATEWAY["upstream_trace_present"]]
                if origin == "gateway":
                    assert present is False
                    assert router.span.parent_span_id is None
                else:
                    assert present is True
                    assert router.span.parent_span_id is not None
        finally:
            for router in cases:
                router.close()


class TestResolvedGatewayParentContract:
    def test_frozen_dataclass_fields(self):
        p = ResolvedGatewayParent(
            trace_id="0af7651916cd43dd8448eb211c80319c",
            parent_span_id="00f067aa0ba902b7",
            origin=ORIGIN_REMOTE_TRACEPARENT,
            upstream_trace_present=True,
        )
        assert p.trace_origin_attribute == "remote"
        with pytest.raises(Exception):
            p.origin = ORIGIN_GATEWAY_ROOT  # frozen

    def test_origin_attribute_mapping(self):
        assert ResolvedGatewayParent("a" * 32, "b" * 16, ORIGIN_SDK_CONTEXT, True).trace_origin_attribute == "sdk"
        assert ResolvedGatewayParent("a" * 32, "b" * 16, ORIGIN_REMOTE_TRACEPARENT, True).trace_origin_attribute == "remote"
        assert ResolvedGatewayParent("a" * 32, None, ORIGIN_GATEWAY_ROOT, False).trace_origin_attribute == "gateway"
