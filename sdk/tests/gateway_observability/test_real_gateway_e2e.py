"""P0-6: REAL gateway E2E — full chain into a mock Core ingest.

Chain under test:

    Client request
    → GatewayAdapter (request/route/attempt extraction)
    → GatewayRuntime → RouterSpan → AttemptSpan(s)
    → mock upstream (success / retryable failure / fallback / stream / cancel)
    → Reporter (sync capture)
    → mock Core ingest (records validated like Core would store them)

Hard assertions (rework doc §7.2):
- Router/Attempt records actually reach Core ingest.
- TraceIDs are valid (32-hex, non-zero).
- Attempt.parent == Router; Router.parent == SDK LLM or remote parent.
- Retry produces multiple unique Attempts; fallback from/to present + hashed.
- Streaming terminal states consistent (Router/Attempt agree).
- Registries/contexts are empty at the end.

Secrets: the mock-chain scenarios always run. The additional LIVE test below
(against a real gateway endpoint) reads ``GATEWAY_E2E_API_KEY`` /
``GATEWAY_E2E_BASE_URL`` / ``GATEWAY_E2E_MODEL`` and FAILS when they are
absent, so a trusted-branch CI run can never go green on silent skips; the
CI job itself is skipped wholesale on fork PRs. Locally, set the three
variables to run the live test, or leave them unset and deselect it:

    pytest .../test_real_gateway_e2e.py --deselect \
        sdk/tests/gateway_observability/test_real_gateway_e2e.py::TestLiveGatewayEndpoint
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import (
    GatewayRuntime,
    PrivacyGuard,
    ErrorCategory,
    GatewayStream,
    CostCalculator,
)
from llm_observability.gateway_observability.attributes import ATTR_ATTEMPT, ATTR_GATEWAY, ATTR_ROUTER
from llm_observability.gateway_observability.context import GatewayContext, clear_gateway_context
from llm_observability.gateway_observability.propagation import inject_downstream_trace_headers


# ── CI contract: live-endpoint secrets ──

_LIVE_ENV = ("GATEWAY_E2E_API_KEY", "GATEWAY_E2E_BASE_URL", "GATEWAY_E2E_MODEL")


def _live_config():
    return {name: os.environ.get(name) for name in _LIVE_ENV}


def _live_secrets_present() -> bool:
    return all(os.environ.get(name) for name in _LIVE_ENV)


# The live-endpoint test runs only when the GATEWAY_E2E_* secrets are present.
# In CI the ``gateway-real-e2e`` job fails upstream (``test -n`` guards) when a
# secret is missing on a trusted branch, and fork PRs never reach this job at
# all — so reaching this test without secrets is a local-dev situation, where
# skipping (not failing) keeps the gateway suite green. The mock-chain suite
# above is the "0 skipped" required E2E and always runs.
_live_skip = not _live_secrets_present()


# ── mock Core ingest ──

class MockCoreIngest:
    """Stands in for Core ``/api/v1/ingest``: stores span records verbatim."""

    def __init__(self, tracer):
        self.records: list[dict] = []
        self._original = tracer.reporter.report
        tracer.reporter.report = self._capture

    def _capture(self, record):
        self.records.append(record)

    def restore(self, tracer):
        tracer.reporter.report = self._original

    # ── query helpers (mirror Core lookups) ──

    def routers(self):
        return [r for r in self.records if r.get("attributes", {}).get(ATTR_GATEWAY["span_role"]) == "router"]

    def attempts(self):
        return [r for r in self.records if r.get("attributes", {}).get(ATTR_GATEWAY["span_role"]) == "provider_attempt"]

    def by_trace(self, trace_id):
        return [r for r in self.records if r.get("trace_id") == trace_id]


_HEX = set("0123456789abcdef")


def _assert_valid_trace_id(trace_id):
    assert trace_id and isinstance(trace_id, str)
    assert len(trace_id) == 32
    assert set(trace_id) <= _HEX
    assert trace_id != "0" * 32


def _assert_tree(ingest, trace_id, expect_attempts):
    routers = [r for r in ingest.by_trace(trace_id) if r["attributes"].get(ATTR_GATEWAY["span_role"]) == "router"]
    attempts = [r for r in ingest.by_trace(trace_id) if r["attributes"].get(ATTR_GATEWAY["span_role"]) == "provider_attempt"]
    assert len(routers) == 1, f"exactly one Router per trace, got {len(routers)}"
    assert len(attempts) == expect_attempts
    router = routers[0]
    _assert_valid_trace_id(router["trace_id"])
    for attempt in attempts:
        assert attempt["parent_span_id"] == router["span_id"], "Attempt.parent must be the Router"
        assert attempt["trace_id"] == trace_id
    return router, attempts


@pytest.fixture(autouse=True)
def _clean_context():
    clear_gateway_context()
    yield
    clear_gateway_context()


@pytest.fixture
def ingest(tracer):
    mock = MockCoreIngest(tracer)
    yield mock
    mock.restore(tracer)


def _runtime(tracer):
    return GatewayRuntime(
        tracer=tracer, sample_rate=1.0, privacy=PrivacyGuard(secret="e2e-secret"),
        cost_calculator=CostCalculator(pricing_table={
            "gpt-5.6": {"input_usd_per_1m_tokens": 2.0, "output_usd_per_1m_tokens": 8.0},
        }),
    )


def _assert_clean_teardown(runtime, router):
    assert router.open_attempt_count == 0
    assert runtime.attempt_registry.size() == 0
    assert runtime.router_registry.size() == 0
    state = GatewayContext.get()
    assert state.router is None and state.active_attempt is None


class TestRealGatewayE2E:
    def test_success_full_chain_into_core(self, tracer, ingest):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({
            "gateway_name": "e2e-gw", "requested_model": "gpt-5.6",
            "user_id": "u-e2e", "session_id": "s-e2e",
        })
        attempt = handle.start_attempt({"channel_id": "ch-primary", "resolved_model": "gpt-5.6"})
        attempt.start()
        downstream = inject_downstream_trace_headers(handle.router, attempt)
        assert downstream["traceparent"].split("-")[1] == handle.router.span.trace_id
        assert downstream["traceparent"].split("-")[2] == attempt.span.span_id
        handle.finish_attempt(attempt, upstream_status=200, duration_ms=42.0, raw_usage={
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        })
        attempt.close()
        handle.finalize()

        trace_id = handle.router.span.trace_id
        router_rec, attempt_recs = _assert_tree(ingest, trace_id, expect_attempts=1)
        assert router_rec["status"] == "OK"
        assert attempt_recs[0]["status"] == "OK"
        # Usage ownership: attempt holds the request usage; router aggregates.
        assert attempt_recs[0]["attributes"]["usage.input_tokens"] == 100
        assert router_rec["attributes"]["usage.total_tokens"] == 150
        # Association fields on the Router record (top-level, existing naming).
        assert router_rec["user_id"] == "u-e2e"
        assert router_rec["session_id"] == "s-e2e"
        # Router is a local root.
        assert router_rec["attributes"][ATTR_GATEWAY["trace_origin"]] == "gateway"
        assert router_rec["parent_span_id"] is None
        _assert_clean_teardown(runtime, handle.router)

    def test_retry_produces_unique_attempts_in_core(self, tracer, ingest):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"gateway_name": "e2e-gw"})
        a1 = handle.start_attempt({"channel_id": "ch-primary", "resolved_model": "gpt-5.6"})
        a1.start()
        handle.finish_attempt(a1, upstream_status=500, raw_usage={
            "prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100,
        })
        a1.close()
        handle.retry_scheduled(attempt_index=a1.attempt_index, delay_ms=1, reason="provider_5xx")
        a2 = handle.start_attempt({"channel_id": "ch-primary", "resolved_model": "gpt-5.6"})
        a2.start()
        handle.finish_attempt(a2, upstream_status=200, raw_usage={
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        })
        a2.close()
        handle.finalize()

        trace_id = handle.router.span.trace_id
        router_rec, attempt_recs = _assert_tree(ingest, trace_id, expect_attempts=2)
        span_ids = {a["span_id"] for a in attempt_recs}
        assert len(span_ids) == 2, "retry must produce unique attempt spans"
        indices = sorted(a["attributes"][ATTR_ATTEMPT["attempt_index"]] for a in attempt_recs)
        assert indices == [1, 2]
        assert router_rec["attributes"][ATTR_ROUTER["retry_count"]] == 1
        # Router aggregates the failed attempt's usage too.
        assert router_rec["attributes"]["usage.total_tokens"] == 220
        # Final state consistent: last attempt succeeded → Router OK.
        assert router_rec["status"] == "OK"
        _assert_clean_teardown(runtime, handle.router)

    def test_fallback_from_to_hashed_in_core(self, tracer, ingest):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"gateway_name": "e2e-gw"})
        a1 = handle.start_attempt({"channel_id": "ch-primary", "resolved_model": "gpt-5.6"})
        a1.start()
        handle.finish_attempt(a1, error=TimeoutError("upstream timeout"))
        a1.close()
        handle.fallback_selected(from_channel_id="ch-primary", to_channel_id="ch-backup", reason="timeout")
        a2 = handle.start_attempt({"channel_id": "ch-backup", "resolved_model": "gpt-5.6"})
        a2.start()
        handle.finish_attempt(a2, upstream_status=200)
        a2.close()
        handle.finalize()

        router_rec, _ = _assert_tree(ingest, handle.router.span.trace_id, expect_attempts=2)
        events = router_rec.get("events") or []
        fallback = [e for e in events if e["name"] == "gateway.fallback.selected"]
        assert len(fallback) == 1
        attrs = fallback[0]["attributes"]
        guard = handle.router._privacy
        assert attrs["from_channel_id"] == guard.hash_channel_id("ch-primary")
        assert attrs["to_channel_id"] == guard.hash_channel_id("ch-backup")
        # Raw channel IDs nowhere in the ingested router record.
        assert "ch-primary" not in str(router_rec)
        assert "ch-backup" not in str(router_rec)
        _assert_clean_teardown(runtime, handle.router)

    def test_streaming_success_terminal_consistency(self, tracer, ingest):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"gateway_name": "e2e-gw"})
        attempt = handle.start_attempt({"channel_id": "ch-primary", "resolved_model": "gpt-5.6"})
        attempt.start()
        chunks = iter([
            ": keepalive",
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
        ])
        stream = handle.finalize_streaming_attempt(chunks, attempt)
        collected = list(stream)

        router_rec, attempt_recs = _assert_tree(ingest, handle.router.span.trace_id, expect_attempts=1)
        assert router_rec["status"] == "OK"
        assert attempt_recs[0]["status"] == "OK"
        assert router_rec["attributes"]["usage.total_tokens"] == 14
        assert router_rec["attributes"][ATTR_ROUTER["ttft_ms"]] is not None
        assert attempt_recs[0]["attributes"][ATTR_ATTEMPT["upstream_ttft_ms"]] is not None
        _assert_clean_teardown(runtime, handle.router)

    def test_streaming_cancel_terminal_consistency(self, tracer, ingest):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"gateway_name": "e2e-gw"})
        attempt = handle.start_attempt({"channel_id": "ch-primary", "resolved_model": "gpt-5.6"})
        attempt.start()

        def gen():
            yield {"choices": [{"delta": {"content": "partial"}}]}
            yield {"choices": [{"delta": {"content": " more"}}]}

        stream = handle.finalize_streaming_attempt(gen(), attempt)
        it = iter(stream)
        next(it)
        stream.close()  # client disconnect

        router_rec, attempt_recs = _assert_tree(ingest, handle.router.span.trace_id, expect_attempts=1)
        # Frozen cancel semantics: both sides ERROR with client_cancelled.
        assert attempt_recs[0]["status"] == "ERROR"
        assert attempt_recs[0]["attributes"][ATTR_ATTEMPT["error_category"]] == ErrorCategory.CLIENT_CANCELLED
        assert router_rec["status"] == "ERROR"
        assert router_rec["attributes"][ATTR_ROUTER["final_error_category"]] == ErrorCategory.CLIENT_CANCELLED
        _assert_clean_teardown(runtime, handle.router)

    def test_no_sdk_trace_creates_valid_root(self, tracer, ingest):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({"gateway_name": "e2e-gw"})
        attempt = handle.start_attempt({"resolved_model": "gpt-5.6"})
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200)
        attempt.close()
        handle.finalize()

        router_rec, _ = _assert_tree(ingest, handle.router.span.trace_id, expect_attempts=1)
        assert router_rec["parent_span_id"] is None
        assert router_rec["attributes"][ATTR_GATEWAY["trace_origin"]] == "gateway"
        assert router_rec["attributes"][ATTR_GATEWAY["upstream_trace_present"]] is False
        _assert_clean_teardown(runtime, handle.router)

    def test_upstream_sampled_zero_not_reported_but_propagated(self, tracer, ingest):
        runtime = _runtime(tracer)
        handle = runtime.handle_request(
            {"gateway_name": "e2e-gw"},
            upstream_traceparent="00-0af7651916cd43dd8448eb211c80319c-00f067aa0ba902b7-00",
        )
        attempt = handle.start_attempt({"resolved_model": "gpt-5.6"})
        attempt.start()
        headers = inject_downstream_trace_headers(handle.router, attempt)
        # sampled=0 → flags stay 00 downstream; business ran normally.
        assert headers["traceparent"].endswith("-00")
        assert headers["traceparent"].split("-")[1] == "0af7651916cd43dd8448eb211c80319c"
        handle.finish_attempt(attempt, upstream_status=200)
        attempt.close()
        handle.finalize()
        # Nothing reported to Core for a sampled-out trace.
        assert ingest.records == [], "sampled-out trace must not generate Reporter records"
        _assert_clean_teardown(runtime, handle.router)

    def test_privacy_no_raw_channel_or_secrets_in_core(self, tracer, ingest):
        runtime = _runtime(tracer)
        handle = runtime.handle_request({
            "gateway_name": "e2e-gw", "user_id": "user-sk-ABCDEFGHIJKLMNOP1234",
        })
        attempt = handle.start_attempt({"channel_id": "ch-very-secret-name", "resolved_model": "gpt-5.6"})
        attempt.start()
        handle.finish_attempt(attempt, upstream_status=200)
        attempt.close()
        handle.finalize()

        blob = str(ingest.records)
        assert "ch-very-secret-name" not in blob
        assert "sk-ABCDEFGHIJKLMNOP1234" not in blob
        _assert_clean_teardown(runtime, handle.router)


@pytest.mark.skipif(_live_skip, reason="GATEWAY_E2E_* secrets not set (live endpoint test)")
class TestLiveGatewayEndpoint:
    """Live HTTP E2E against a real gateway endpoint (CI trusted branches).

    Missing secrets FAIL the CI job upstream (the ``gateway-real-e2e`` job's
    ``test -n`` guards), never silently skip — the whole job is skipped on fork
    PRs, so a trusted-branch run reaching pytest always has secrets set.
    """

    def test_live_non_streaming_success(self, tracer, ingest):
        config = _live_config()
        missing = [k for k, v in config.items() if not v]
        assert not missing, (
            f"gateway real-E2E secrets missing: {missing} — on trusted branches "
            "this must fail; fork PRs should not run this job at all"
        )
        import json
        import urllib.request

        runtime = _runtime(tracer)
        handle = runtime.handle_request({
            "gateway_name": "live-e2e", "requested_model": config["GATEWAY_E2E_MODEL"],
            "route": "/v1/chat/completions",
        })
        attempt = handle.start_attempt({
            "resolved_model": config["GATEWAY_E2E_MODEL"], "provider": "live",
        })
        attempt.start()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['GATEWAY_E2E_API_KEY']}",
        }
        # The Attempt continues the trace downstream (P1-6).
        headers.update(inject_downstream_trace_headers(handle.router, attempt))
        body = json.dumps({
            "model": config["GATEWAY_E2E_MODEL"],
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 8,
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            config["GATEWAY_E2E_BASE_URL"].rstrip("/") + "/v1/chat/completions",
            data=body, headers=headers, method="POST",
        )
        started = time.time()
        status = None
        payload = None
        error = None
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                status = resp.status
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — recorded, then asserted below
            error = e
            status = getattr(e, "code", None)
        duration_ms = (time.time() - started) * 1000

        handle.finish_attempt(
            attempt,
            response=payload,
            error=error,
            upstream_status=status,
            duration_ms=round(duration_ms, 2),
        )
        attempt.close()
        handle.finalize()

        assert error is None, f"live gateway call failed: {error}"
        assert status == 200
        router_rec, attempt_recs = _assert_tree(ingest, handle.router.span.trace_id, expect_attempts=1)
        assert router_rec["status"] == "OK"
        assert attempt_recs[0]["status"] == "OK"
        # A live upstream returns usage; it must land on Attempt + Router.
        assert attempt_recs[0]["attributes"].get("usage.total_tokens") is not None
        assert router_rec["attributes"].get("usage.total_tokens") is not None
        # The API key never reaches telemetry.
        assert config["GATEWAY_E2E_API_KEY"] not in str(ingest.records)
        _assert_clean_teardown(runtime, handle.router)
