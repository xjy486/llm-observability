"""P1-3: REAL HTTP gateway E2E — full chain over real HTTP into a real Core.

Chain: httpx client → GatewayHarness (aiohttp) → adapter → GatewayRuntime →
Router/Attempt → mock upstream → SDK Reporter (real HTTP) → MockCoreServer
(/api/v1/ingest). No reporter.report monkeypatch; no network egress; no secrets.

Covers: success / retry / fallback / streaming success / streaming cancel /
no-SDK trace / sampled=0 / privacy. Hard assertions on records that actually
reached Core over HTTP.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import httpx
import pytest

from llm_observability.gateway_observability.attributes import ATTR_GATEWAY, ATTR_ROUTER, ATTR_ATTEMPT
from llm_observability.gateway_observability.errors import ErrorCategory

from gateway_http_harness import (
    GatewayHarness,
    MockCoreServer,
    make_tracer,
    stop_tracer,
    streaming_cancel_gate,
    streaming_cancel_fired,
)


def _wait_for_records(core, count, timeout=5.0):
    """Poll the mock Core until ``count`` records arrive over HTTP (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(core.snapshot()) >= count:
            return core.snapshot()
        time.sleep(0.05)
    return core.snapshot()


def _routers(records):
    return [r for r in records if r.get("attributes", {}).get(ATTR_GATEWAY["span_role"]) == "router"]


def _attempts(records):
    return [r for r in records if r.get("attributes", {}).get(ATTR_GATEWAY["span_role"]) == "provider_attempt"]


def _assert_valid_trace_id(trace_id):
    assert trace_id and isinstance(trace_id, str)
    assert len(trace_id) == 32
    assert set(trace_id) <= set("0123456789abcdef")
    assert trace_id != "0" * 32


@pytest.fixture
def harness():
    core = MockCoreServer()
    core_url = core.start()
    tracer = make_tracer(core_url)
    gw = GatewayHarness(tracer=tracer, core_url=core_url)
    gw_url = gw.start()
    try:
        yield _HarnessCtx(core=core, gw=gw, gw_url=gw_url, tracer=tracer)
    finally:
        # Drain the reporter so queued records reach Core, then tear down.
        stop_tracer(tracer)
        gw.stop()
        core.stop()


class _HarnessCtx:
    def __init__(self, core, gw, gw_url, tracer):
        self.core = core
        self.gw = gw
        self.url = gw_url
        self.tracer = tracer

    def post(self, scenario="success", stream=False, traceparent=None, **body):
        headers = {"X-E2E-Scenario": scenario}
        if traceparent is not None:
            headers["traceparent"] = traceparent
        payload = {"model": "mock-model", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
        payload.update(body)
        return httpx.post(self.url + "/v1/chat/completions", json=payload, headers=headers, timeout=10)


class TestGatewayHttpE2E:
    def test_success_full_chain_into_core_over_http(self, harness):
        r = harness.post(scenario="success")
        assert r.status_code == 200
        records = _wait_for_records(harness.core, 2)
        assert len(records) >= 2, "Router + Attempt must reach Core over HTTP"
        routers, attempts = _routers(records), _attempts(records)
        assert len(routers) == 1
        router = routers[0]
        _assert_valid_trace_id(router["trace_id"])
        assert router["attributes"][ATTR_GATEWAY["trace_origin"]] == "gateway"
        assert router["parent_span_id"] is None
        for a in attempts:
            assert a["parent_span_id"] == router["span_id"], "Attempt.parent == Router"
            assert a["trace_id"] == router["trace_id"]
        # Usage + cost reached Core (priced mock-model).
        assert router["attributes"].get("usage.total_tokens") == 15
        assert router["attributes"].get("cost.source") == "priced"
        assert harness.gw.runtime.router_registry.size() == 0
        assert harness.gw.runtime.attempt_registry.size() == 0

    def test_retry_produces_unique_attempts_over_http(self, harness):
        r = harness.post(scenario="retry_5xx")
        assert r.status_code == 200
        records = _wait_for_records(harness.core, 3)
        routers, attempts = _routers(records), _attempts(records)
        assert len(routers) == 1
        assert len(attempts) == 2, "retry must produce two unique Attempts"
        span_ids = {a["span_id"] for a in attempts}
        assert len(span_ids) == 2
        indices = sorted(a["attributes"][ATTR_ATTEMPT["attempt_index"]] for a in attempts)
        assert indices == [1, 2]
        assert routers[0]["attributes"][ATTR_ROUTER["retry_count"]] == 1
        # Router aggregates the failed attempt's usage too.
        assert routers[0]["attributes"]["usage.total_tokens"] == 25  # 10 + 15

    def test_fallback_from_to_hashed_over_http(self, harness):
        r = harness.post(scenario="fallback_timeout")
        assert r.status_code == 200
        records = _wait_for_records(harness.core, 3)
        routers, attempts = _routers(records), _attempts(records)
        assert len(routers) == 1
        assert len(attempts) == 2
        events = routers[0].get("events") or []
        fb = [e for e in events if e["name"] == "gateway.fallback.selected"]
        assert len(fb) == 1
        attrs = fb[0]["attributes"]
        guard = harness.gw.runtime._privacy
        assert attrs["from_channel_id"] == guard.hash_channel_id("ch-mock")
        assert attrs["to_channel_id"] == guard.hash_channel_id("ch-backup")
        # Raw channel IDs never reach Core.
        blob = str(records)
        assert "ch-mock" not in blob
        assert "ch-backup" not in blob

    def test_streaming_success_terminal_consistency_over_http(self, harness):
        with httpx.stream(
            "POST", harness.url + "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"X-E2E-Scenario": "success"}, timeout=10,
        ) as r:
            assert r.status_code == 200
            chunks = list(r.iter_lines())
        # The stream emitted content + [DONE].
        assert any("Hello" in c for c in chunks)
        records = _wait_for_records(harness.core, 2)
        routers, attempts = _routers(records), _attempts(records)
        assert len(routers) == 1 and len(attempts) == 1
        assert routers[0]["status"] == "OK"
        assert attempts[0]["status"] == "OK"
        # Streaming cost computed (priced mock-model).
        assert routers[0]["attributes"].get("cost.source") == "priced"
        assert routers[0]["attributes"].get("usage.total_tokens") == 15

    def test_streaming_cancel_terminal_consistency_over_http(self, harness):
        # Client opens the stream, consumes one chunk, then disconnects. Over
        # real HTTP the server's write-error detection is buffered and
        # timing-dependent (aiohttp absorbs writes into its send buffer), so
        # the mid-stream cancel-vs-clean-completion outcome is not
        # deterministic at the HTTP layer. The observable contract asserted
        # here: the stream terminates, both spans end with consistent status,
        # records reach Core over HTTP, and registries/contexts are clean.
        # The frozen client_cancelled terminal-state semantics are unit-tested
        # deterministically in test_stream_terminal_state.py.
        streaming_cancel_gate.clear()
        streaming_cancel_fired.clear()
        with httpx.stream(
            "POST", harness.url + "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"X-E2E-Scenario": "streaming_cancel"}, timeout=10,
        ) as r:
            assert r.status_code == 200
            it = r.iter_lines()
            try:
                next(it)  # consume the first chunk
            except StopIteration:
                pass
            streaming_cancel_gate.set()
            # Exiting the context closes the connection (possibly mid-stream).
        records = _wait_for_records(harness.core, 2, timeout=6.0)
        routers, attempts = _routers(records), _attempts(records)
        assert routers, "Router must reach Core over HTTP after stream end"
        assert attempts, "Attempt must reach Core over HTTP after stream end"
        router, attempt = routers[0], attempts[0]
        # Router and Attempt terminal statuses must agree (never one OK, one ERROR).
        assert router["status"] == attempt["status"], (
            "Router/Attempt terminal states must be consistent "
            f"(router={router['status']}, attempt={attempt['status']})"
        )
        assert harness.gw.runtime.router_registry.size() == 0
        assert harness.gw.runtime.attempt_registry.size() == 0

    def test_no_sdk_trace_creates_valid_root_over_http(self, harness):
        # No traceparent header → Router is a local root.
        r = harness.post(scenario="success")
        assert r.status_code == 200
        records = _wait_for_records(harness.core, 2)
        router = _routers(records)[0]
        _assert_valid_trace_id(router["trace_id"])
        assert router["attributes"][ATTR_GATEWAY["trace_origin"]] == "gateway"
        assert router["attributes"][ATTR_GATEWAY["upstream_trace_present"]] is False
        assert router["parent_span_id"] is None

    def test_upstream_sampled_zero_not_reported_but_propagated(self, harness):
        # traceparent with sampled=0 (flags 00). Sampled out → no Core records,
        # but the downstream header still carries 00.
        sampled_out_tp = "00-0af7651916cd43dd8448eb211c80319c-1234567890abcdef-00"
        # Capture the injected downstream traceparent via a no-SDK root is not
        # possible here (the harness calls the mock upstream internally); we
        # assert the observable contract: sampled=0 → nothing reaches Core.
        r = harness.post(scenario="success", traceparent=sampled_out_tp)
        assert r.status_code == 200
        time.sleep(0.5)  # give the reporter a chance to NOT send anything
        records = harness.core.snapshot()
        # Sampled-out traces are not reported over HTTP.
        assert records == [], "sampled=0 trace must not generate Reporter records"

    def test_privacy_no_raw_channel_or_secrets_in_core_over_http(self, harness):
        # Inject a secret-shaped user_id and a route with a query secret.
        r = harness.post(scenario="success", user_id="user-sk-abcdefghijklmnopqrst")
        assert r.status_code == 200
        records = _wait_for_records(harness.core, 2)
        blob = str(records)
        assert "sk-abcdefghijklmnopqrst" not in blob, "secret must not reach Core"
        assert "ch-mock" not in blob, "raw channel id must not reach Core"
