"""Blocker 2: the unified privacy guard is wired into the real runtime path.

Drives malicious values through the real GatewayRuntime → RouterSpan/AttemptSpan
(not a bare hand-made span) and asserts the guard strips query secrets, truncates
oversized values, masks secrets, and rejects unknown keys.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability.attributes import ATTR_GATEWAY, ATTR_ROUTER, ATTR_ATTEMPT
from llm_observability.gateway_observability.context import clear_gateway_context
from llm_observability.gateway_observability.errors import ErrorCategory, GatewayError
from llm_observability.gateway_observability.runtime import GatewayRuntime


# A secret-shaped token long enough to match the sk- pattern (>= 16 alnums).
_SK = "sk-abcdefghijklmnopqrst"


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


def _router(tracer):
    return GatewayRuntime(tracer=tracer, sample_rate=1.0)


class TestRuntimePrivacyIntegration:
    def test_runtime_route_query_removed(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({
            "gateway_name": "gw",
            "route": f"/v1/chat/completions?api_key={_SK}",
        })
        recorded = handle.router.span.attributes.get(ATTR_GATEWAY["route"])
        assert recorded is not None
        assert _SK not in recorded, "raw secret in route query must be stripped"
        assert "api_key" not in recorded, "query must be removed from route"
        handle.finalize()

    def test_runtime_request_id_size_limited(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({
            "gateway_name": "gw",
            "request_id": "x" * 10000,
        })
        recorded = handle.router.span.attributes.get(ATTR_GATEWAY["request_id"])
        assert recorded is not None
        assert len(recorded.encode("utf-8")) <= 256, "request_id must be <= 256 bytes"
        handle.finalize()

    def test_runtime_route_reason_secret_redacted(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({
            "gateway_name": "gw",
            "route_reason": f"chosen because key={_SK}",
        })
        recorded = handle.router.span.attributes.get(ATTR_ROUTER["route_reason"])
        assert recorded is not None
        assert _SK not in recorded, "secret in route_reason must be redacted"
        assert "<redacted>" in recorded
        handle.finalize()

    def test_runtime_upstream_request_id_sanitized(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({"gateway_name": "gw"})
        attempt = handle.start_attempt({"provider": "openai"})
        attempt.start()
        attempt.set_upstream_request_id(f"req-{_SK}-trail")
        recorded = attempt.span.attributes.get(ATTR_ATTEMPT["upstream_request_id"])
        assert recorded is not None
        assert _SK not in recorded, "secret in upstream_request_id must be redacted"
        assert "<redacted>" in recorded
        attempt.close()
        handle.finalize()

    def test_runtime_attempt_error_message_redacted(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({"gateway_name": "gw"})
        attempt = handle.start_attempt({"provider": "openai"})
        attempt.start()
        attempt.set_error(GatewayError(
            category=ErrorCategory.STREAM_INTERRUPTED,
            type="StreamError",
            message=f"upstream reset: Bearer {_SK} leaked",
            retryable=True,
        ))
        recorded = attempt.span.attributes.get(ATTR_ATTEMPT["error_message"])
        assert recorded is not None
        assert _SK not in recorded, "secret in error_message must be redacted"
        assert "<redacted>" in recorded
        attempt.close()
        handle.finalize()

    def test_runtime_unknown_gateway_attribute_rejected(self, tracer):
        runtime = _router(tracer)
        handle = runtime.handle_request({"gateway_name": "gw"})
        attempt = handle.start_attempt({"provider": "openai"})
        attempt.start()
        # The real AttemptSpan guarded path: an unknown key is denied.
        wrote = attempt._set_attr(attempt.span, "gateway.attacker_field", "evil-value")
        assert wrote is False
        assert "gateway.attacker_field" not in attempt.span.attributes
        assert "evil-value" not in str(attempt.span.attributes)
        attempt.close()
        handle.finalize()
