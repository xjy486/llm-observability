"""Router span lifecycle + hierarchy tests (spec §4, §9.2)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    GenericAdapter,
    PrivacyGuard,
    GatewayRequestContext,
    RouteDecision,
    GatewayContext,
    RouterSpan,
)
from llm_observability.gateway_observability.registry import RouterRegistry, AttemptRegistry
from llm_observability.context import get_current_context


def _make_runtime(**kwargs):
    kwargs.setdefault("sample_rate", 1.0)
    kwargs.setdefault("privacy", PrivacyGuard(secret="test-secret"))
    return GatewayRuntime(**kwargs)


def test_router_span_role_and_generic_attrs(clean_sdk):
    rt = _make_runtime()
    handle = rt.handle_request({
        "gateway_name": "mock",
        "request_id": "req-1",
        "requested_model": "gpt-5.6",
        "route": "/v1/chat/completions",
        "protocol": "openai-compatible",
    })
    router = handle.router
    assert router.span.attributes["gateway.span_role"] == "router"
    assert router.span.attributes["gateway.name"] == "mock"
    assert router.span.attributes["gateway.trace_origin"] == "gateway"
    assert router.span.attributes["gateway.upstream_trace_present"] is False
    handle.finalize()


def test_router_is_root_without_sdk_context(clean_sdk):
    """No upstream SDK trace → Router is Root (no fabricated LLM/AGENT)."""
    rt = _make_runtime()
    handle = rt.handle_request({"gateway_name": "mock"})
    router = handle.router
    assert router.span.parent_span_id is None
    assert router.span.span_kind == "GATEWAY"
    handle.finalize()


def test_router_parent_is_llm_span_with_sdk_context(clean_sdk, tracer):
    """Router parent = SDK LLM span when present."""
    with tracer.trace(name="root"):
        from llm_observability.integrations.langchain.llm_span import LogicalLLMSpan
        with LogicalLLMSpan(request=None) as llm_handle:
            rt = _make_runtime()
            handle = rt.handle_request({"gateway_name": "mock"})
            router = handle.router
            llm_span = llm_handle._span
            assert router.span.parent_span_id == llm_span.span_id
            assert router.span.trace_id == llm_span.trace_id
            handle.finalize()


def test_router_duration_metrics_and_attempt_counts(clean_sdk):
    rt = _make_runtime()
    handle = rt.handle_request({"gateway_name": "mock", "requested_model": "gpt-5.6"})
    router = handle.router
    a1 = handle.start_attempt({"attempt_index": 1, "provider": "openai", "channel_id": "ch-1"})
    a1.start()
    handle.finish_attempt(a1, upstream_status=200, duration_ms=100.0,
                         raw_usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})
    a1.close()
    handle.finalize()

    attrs = router.span.attributes
    assert attrs["gateway.attempt_count"] == 1
    assert attrs["gateway.retry_count"] == 0
    assert attrs["gateway.fallback_count"] == 0
    assert attrs["gateway.total_duration_ms"] >= 0
    assert attrs["gateway.final_http_status_code"] == 200
    assert attrs["usage.input_tokens"] == 5
    assert attrs["usage.total_tokens"] == 8


def test_router_context_cleared_after_close(clean_sdk):
    rt = _make_runtime()
    handle = rt.handle_request({"gateway_name": "mock"})
    assert GatewayContext.get().router is not None
    handle.finalize()
    state = GatewayContext.get()
    assert state.router is None


def test_router_registry_cleanup_after_close(clean_sdk):
    reg = RouterRegistry()
    rt = _make_runtime(router_registry=reg)
    handle = rt.handle_request({"gateway_name": "mock"})
    assert reg.size() == 1
    handle.finalize()
    assert reg.size() == 0
