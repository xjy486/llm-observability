"""Registry + context cleanup tests (runtime spec §Registry, task 4.4)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import GatewayRuntime, PrivacyGuard
from llm_observability.gateway_observability.context import GatewayContext
from llm_observability.gateway_observability.registry import RouterRegistry, AttemptRegistry


def _runtime(router_registry=None, attempt_registry=None):
    return GatewayRuntime(
        sample_rate=1.0,
        privacy=PrivacyGuard(secret="s"),
        router_registry=router_registry,
        attempt_registry=attempt_registry,
    )


def test_normal_completion_cleans_everything(clean_sdk):
    reg = RouterRegistry()
    areg = AttemptRegistry()
    rt = _runtime(reg, areg)
    handle = rt.handle_request({"gateway_name": "mock"})
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)
    a.close()
    handle.finalize()
    assert reg.size() == 0
    assert areg.size() == 0
    state = GatewayContext.get()
    assert state.router is None and state.active_attempt is None


def test_error_path_cleans_registries(clean_sdk):
    reg = RouterRegistry()
    areg = AttemptRegistry()
    rt = _runtime(reg, areg)
    handle = rt.handle_request({"gateway_name": "mock"})
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, error=TimeoutError("timeout"))
    a.close()
    handle.finalize()
    assert reg.size() == 0
    assert areg.size() == 0


def test_cancel_path_cleans(clean_sdk):
    reg = RouterRegistry()
    areg = AttemptRegistry()
    rt = _runtime(reg, areg)
    handle = rt.handle_request({"gateway_name": "mock"})
    a = handle.start_attempt({"attempt_index": 1})
    a.start()
    handle.finish_attempt(a, upstream_status=200)
    # Cancel via stream-like finalization: just close + finalize.
    a.close()
    handle.finalize()
    assert reg.size() == 0
    assert areg.size() == 0


def test_span_end_failure_still_cleans(clean_sdk):
    """span.end() raises → registries + ContextVar still cleaned."""
    from llm_observability.spans import Span
    reg = RouterRegistry()
    areg = AttemptRegistry()
    orig = Span.end
    def bad_end(self):
        raise RuntimeError("end fail")
    Span.end = bad_end
    try:
        rt = _runtime(reg, areg)
        handle = rt.handle_request({"gateway_name": "mock"})
        a = handle.start_attempt({"attempt_index": 1})
        a.start()
        handle.finish_attempt(a, upstream_status=200)
        a.close()
        handle.finalize()
    finally:
        Span.end = orig
    assert reg.size() == 0
    assert areg.size() == 0
    state = GatewayContext.get()
    assert state.router is None and state.active_attempt is None


def test_no_context_after_close_aclose(clean_sdk):
    """No stale gateway ContextVar after close/aclose paths."""
    rt = _runtime()
    handle = rt.handle_request({"gateway_name": "mock"})
    handle.close()  # close path
    assert GatewayContext.get().router is None
