"""
Phase 2.1 Closeout tests — P0 and P1 fix verification.

Covers:
  P0-1: Reporter lifecycle auto-managed by Public SDK
  P0-2: OpenAI Instrumentor single-instance lifecycle (init/shutdown/re-init)
  P0-3: Streaming LLM Span lifecycle covers full stream
  P0-4: Token ownership — no double counting (LLM only)
  P1-1: Nested trace raises error
  P1-2: sample_rate and api_key enforcement
  P1-3: SDK/Proxy masking consistency
  P1-4: Dedup still propagates traceparent + ownership marker
  P1-5: Internal headers stripped from upstream request

Run:  pytest tests/test_phase2_1_closeout.py -v
"""
import sys
import os
import time
import json
import tempfile
import threading

# Path setup
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "core"))
sys.path.insert(0, os.path.join(ROOT, "proxy"))

import pytest
from llm_observability import Observability
from llm_observability.reporter import Reporter
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context
from llm_observability.spans import Span, SpanKind
from llm_observability.propagation import inject_headers, inject_traceparent
from llm_observability.utils.masking import mask_payload, _mask_string_patterns
from llm_observability.instrumentation.openai import OpenAIInstrumentor, ObservedStream

from storage.db import Storage
from config import ProxyConfig


# ──────────────────────────────────────────────
# P0-1: Reporter Lifecycle Auto-Managed
# ──────────────────────────────────────────────

class TestP01ReporterLifecycle:
    """P0-1: Reporter must be auto-started by Observability.init()."""

    def setup_method(self):
        """Clean state before each test."""
        if Observability._initialized:
            Observability.shutdown()

    def teardown_method(self):
        """Clean state after each test."""
        if Observability._initialized:
            Observability.shutdown()

    def test_init_auto_starts_reporter(self):
        """init() must auto-start the Reporter background thread."""
        Observability.init(
            app_name="test-app",
            endpoint="http://localhost:99999",  # non-existent, won't actually connect
        )
        # Reporter should have a running background thread
        assert Observability._reporter is not None
        assert Observability._reporter._thread is not None
        assert Observability._reporter._thread.is_alive()
        # The event loop should be running
        assert Observability._reporter._loop is not None

    def test_shutdown_stops_reporter_thread(self):
        """shutdown() must stop the Reporter background thread."""
        Observability.init(
            app_name="test-app",
            endpoint="http://localhost:99999",
        )
        thread = Observability._reporter._thread
        assert thread.is_alive()

        Observability.shutdown()

        # Thread should no longer be alive
        assert not thread.is_alive()
        # State should be reset
        assert Observability._reporter is None

    def test_report_accepts_data_after_init(self):
        """After init(), report() should accept data without error."""
        Observability.init(
            app_name="test-app",
            endpoint="http://localhost:99999",
        )
        # This should not raise
        Observability._reporter.report({"trace_id": "test", "span_id": "s1"})
        # Data should be in the queue
        assert len(Observability._reporter._queue) == 1

    def test_init_shutdown_reinit(self):
        """init → shutdown → re-init should work cleanly."""
        Observability.init(endpoint="http://localhost:99999")
        assert Observability._initialized

        Observability.shutdown()
        assert not Observability._initialized

        Observability.init(endpoint="http://localhost:99999")
        assert Observability._initialized
        assert Observability._reporter._thread.is_alive()

        Observability.shutdown()


# ──────────────────────────────────────────────
# P0-2: OpenAI Instrumentor Lifecycle
# ──────────────────────────────────────────────

class TestP02InstrumentorLifecycle:
    """P0-2: Single instrumentor instance across init/shutdown/re-init."""

    def setup_method(self):
        if Observability._initialized:
            Observability.shutdown()

    def teardown_method(self):
        if Observability._initialized:
            Observability.shutdown()

    def test_init_holds_instrumentor_instance(self):
        """init() should create and hold a single instrumentor instance."""
        try:
            import openai
        except ImportError:
            pytest.skip("openai not installed")

        Observability.init(endpoint="http://localhost:99999", auto_instrument_openai=True)
        assert Observability._openai_instrumentor is not None
        assert Observability._openai_instrumentor._patched is True

    def test_shutdown_uninstruments_same_instance(self):
        """shutdown() must uninstrument using the SAME instance."""
        try:
            import openai
        except ImportError:
            pytest.skip("openai not installed")

        Observability.init(endpoint="http://localhost:99999", auto_instrument_openai=True)
        instrumentor = Observability._openai_instrumentor

        Observability.shutdown()

        # The same instance should be unpatched
        assert instrumentor._patched is False
        assert Observability._openai_instrumentor is None

    def test_reinit_uses_new_instance(self):
        """Re-init after shutdown should create a new instrumentor instance."""
        try:
            import openai
        except ImportError:
            pytest.skip("openai not installed")

        Observability.init(endpoint="http://localhost:99999", auto_instrument_openai=True)
        first = Observability._openai_instrumentor

        Observability.shutdown()

        Observability.init(endpoint="http://localhost:99999", auto_instrument_openai=True)
        second = Observability._openai_instrumentor

        assert first is not second
        assert second._patched is True

        Observability.shutdown()

    def test_no_double_patch_on_reinit(self):
        """Re-init should not cause double patching."""
        try:
            import openai
        except ImportError:
            pytest.skip("openai not installed")

        target = openai.resources.chat.completions.Completions

        # Save original
        original = target.create

        Observability.init(endpoint="http://localhost:99999", auto_instrument_openai=True)
        patched_1 = target.create
        assert patched_1 is not original

        Observability.shutdown()
        # After shutdown, should be restored
        assert target.create is original

        Observability.init(endpoint="http://localhost:99999", auto_instrument_openai=True)
        patched_2 = target.create
        # Should be patched again (different wrapper function)
        assert patched_2 is not original

        Observability.shutdown()
        # Should be restored again
        assert target.create is original


# ──────────────────────────────────────────────
# P0-3: Streaming LLM Span Lifecycle
# ──────────────────────────────────────────────

class TestP03StreamingLifecycle:
    """P0-3: Streaming span must cover the full stream consumption."""

    def test_observed_stream_finalizes_on_exhaustion(self):
        """ObservedStream should finalize span when iterator is exhausted."""
        # Create a mock stream
        class MockChunk:
            def __init__(self, content):
                self.choices = [type("Choice", (), {
                    "delta": type("Delta", (), {"content": content})()
                })()]

        class MockStream:
            def __init__(self, chunks):
                self._chunks = chunks
                self._index = 0
            def __iter__(self):
                return self
            def __next__(self):
                if self._index >= len(self._chunks):
                    raise StopIteration
                chunk = self._chunks[self._index]
                self._index += 1
                return chunk

        chunks = [MockChunk("hello"), MockChunk(" world")]
        mock_stream = MockStream(chunks)

        # Create span and tracer mock
        span = Span(
            trace_id="t1", span_id="s1", parent_span_id=None,
            span_name="llm.completion", span_kind=SpanKind.LLM,
        )
        span.start()

        class MockTracer:
            class MockConfig:
                payload_strategy = "masked"
            config = MockConfig()
            class MockReporter:
                reported = []
                def report(self, record):
                    self.reported.append(record)
            reporter = MockReporter()

        tracer = MockTracer()

        observed = ObservedStream(mock_stream, span, tracer, sampled=True)

        # Consume the stream
        collected = []
        for chunk in observed:
            collected.append(chunk.choices[0].delta.content)

        assert collected == ["hello", " world"]
        # Span should be finalized
        assert observed._finalized is True
        assert span.status == "OK"
        assert span.end_time > 0
        # Should be reported
        assert len(tracer.reporter.reported) == 1

    def test_observed_stream_finalizes_on_error(self):
        """ObservedStream should set ERROR status when iteration fails."""
        class ErrorStream:
            def __iter__(self):
                return self
            def __next__(self):
                raise ConnectionError("stream interrupted")

        span = Span(
            trace_id="t1", span_id="s1", parent_span_id=None,
            span_name="llm.completion", span_kind=SpanKind.LLM,
        )
        span.start()

        class MockTracer:
            class MockConfig:
                payload_strategy = "masked"
            config = MockConfig()
            class MockReporter:
                reported = []
                def report(self, record):
                    self.reported.append(record)
            reporter = MockReporter()

        tracer = MockTracer()

        observed = ObservedStream(ErrorStream(), span, tracer, sampled=True)

        with pytest.raises(ConnectionError):
            for _ in observed:
                pass

        assert observed._finalized is True
        assert span.status == "ERROR"
        assert span.error_type == "ConnectionError"

    def test_observed_stream_close_finalizes(self):
        """close() should finalize the span."""
        class MockChunk:
            def __init__(self, content):
                self.choices = [type("Choice", (), {
                    "delta": type("Delta", (), {"content": content})()
                })()]

        class MockStream:
            def __init__(self):
                self._called_close = False
            def __iter__(self):
                return iter([MockChunk("partial")])
            def close(self):
                self._called_close = True

        mock_stream = MockStream()
        span = Span(
            trace_id="t1", span_id="s1", parent_span_id=None,
            span_name="llm.completion", span_kind=SpanKind.LLM,
        )
        span.start()

        class MockTracer:
            class MockConfig:
                payload_strategy = "masked"
            config = MockConfig()
            class MockReporter:
                reported = []
                def report(self, record):
                    self.reported.append(record)
            reporter = MockReporter()

        tracer = MockTracer()

        observed = ObservedStream(mock_stream, span, tracer, sampled=True)
        observed.close()

        assert observed._finalized is True
        assert mock_stream._called_close is True


# ──────────────────────────────────────────────
# P0-4: Token Ownership — No Double Counting
# ──────────────────────────────────────────────

def _make_span_record(
    trace_id="trace-1", span_id="span-1", span_kind="LLM",
    status="OK", duration_ms=100.0, model="gpt-4",
    start_time=None, total_tokens=30, input_tokens=10, output_tokens=20,
    parent_span_id=None,
):
    """Create a span record for Storage.insert_span()."""
    if start_time is None:
        start_time = time.time() - 60
    attrs = {
        "gen_ai.request.model": model,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.total_tokens": total_tokens,
    }
    return {
        "trace_id": trace_id, "span_id": span_id,
        "parent_span_id": parent_span_id,
        "span_kind": span_kind, "span_name": "llm.completion",
        "start_time": start_time,
        "end_time": start_time + duration_ms / 1000.0,
        "duration_ms": duration_ms,
        "status": status, "ttft_ms": None, "first_chunk_ms": None,
        "session_id": "sess-1", "user_id": "user-1",
        "app_name": "test-app", "business_scene": None,
        "attributes": attrs, "events": [],
        "payload": None, "request_metadata": None,
    }


class TestP04TokenOwnership:
    """P0-4: Token aggregation must only count LLM spans, not GATEWAY."""

    def test_trace_total_tokens_llm_only(self):
        """Trace with LLM=30 and GATEWAY=30 should have total_tokens=30."""
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        # 1 AGENT + 1 LLM(total=30) + 1 GATEWAY(total=30)
        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="agent-1", span_kind="AGENT",
            total_tokens=0, input_tokens=0, output_tokens=0,
            start_time=base, duration_ms=500,
        ))
        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="llm-1", span_kind="LLM",
            total_tokens=30, input_tokens=10, output_tokens=20,
            start_time=base + 0.1, duration_ms=200,
            parent_span_id="agent-1",
        ))
        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="gw-1", span_kind="GATEWAY",
            total_tokens=30, input_tokens=10, output_tokens=20,
            start_time=base + 0.2, duration_ms=180,
            parent_span_id="llm-1",
        ))

        result = storage.get_trace_summaries()
        trace = result["traces"][0]
        assert trace["total_tokens"] == 30, \
            f"Expected 30 (LLM only), got {trace['total_tokens']}"
        assert trace["input_tokens"] == 10
        assert trace["output_tokens"] == 20
        assert trace["llm_call_count"] == 1
        assert trace["span_count"] == 3

    def test_metrics_tokens_llm_only(self):
        """Metrics total_tokens should only count LLM spans."""
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="llm-1", span_kind="LLM",
            total_tokens=30, start_time=base,
        ))
        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="gw-1", span_kind="GATEWAY",
            total_tokens=30, start_time=base + 0.1,
        ))

        metrics = storage.get_metrics()
        assert metrics["total_tokens"] == 30, \
            f"Expected 30 (LLM only), got {metrics['total_tokens']}"
        assert metrics["llm_call_count"] == 1

    def test_trace_detail_tokens_llm_only(self):
        """Trace detail total_tokens should only count LLM spans."""
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="llm-1", span_kind="LLM",
            total_tokens=30, start_time=base,
        ))
        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="gw-1", span_kind="GATEWAY",
            total_tokens=30, start_time=base + 0.1,
            parent_span_id="llm-1",
        ))

        detail = storage.get_trace_detail("t1")
        assert detail["total_tokens"] == 30
        assert detail["llm_call_count"] == 1

    def test_time_series_tokens_llm_only(self):
        """Time series tokens should only count LLM spans."""
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="llm-1", span_kind="LLM",
            total_tokens=30, start_time=base,
        ))
        storage.insert_span(_make_span_record(
            trace_id="t1", span_id="gw-1", span_kind="GATEWAY",
            total_tokens=30, start_time=base,
        ))

        ts = storage.get_time_series(base - 10, base + 10, interval_seconds=60)
        assert len(ts) >= 1
        assert ts[0]["tokens"] == 30, \
            f"Expected 30 (LLM only), got {ts[0]['tokens']}"


# ──────────────────────────────────────────────
# P1-1: Nested Trace
# ──────────────────────────────────────────────

class TestP11NestedTrace:
    """P1-1: Nested trace() must raise an error."""

    def setup_method(self):
        if Observability._initialized:
            Observability.shutdown()

    def teardown_method(self):
        if Observability._initialized:
            Observability.shutdown()

    def test_nested_trace_raises(self):
        """Calling trace() inside an existing trace should raise RuntimeError."""
        Observability.init(endpoint="http://localhost:99999")

        with Observability.trace(name="outer"):
            with pytest.raises(RuntimeError, match="Nested trace"):
                with Observability.trace(name="inner"):
                    pass


# ──────────────────────────────────────────────
# P1-2: sample_rate and api_key
# ──────────────────────────────────────────────

class TestP12SampleRateApiKey:
    """P1-2: sample_rate and api_key must be enforced."""

    def setup_method(self):
        if Observability._initialized:
            Observability.shutdown()

    def teardown_method(self):
        if Observability._initialized:
            Observability.shutdown()

    def test_sample_rate_zero_no_report(self):
        """sample_rate=0 should not report spans."""
        Observability.init(endpoint="http://localhost:99999", sample_rate=0.0)

        with Observability.trace(name="test"):
            pass

        # Queue should be empty (not sampled, not reported)
        assert len(Observability._reporter._queue) == 0

    def test_sample_rate_one_reports(self):
        """sample_rate=1.0 should report spans."""
        Observability.init(endpoint="http://localhost:99999", sample_rate=1.0)

        with Observability.trace(name="test"):
            pass

        # Queue should have 1 span (sampled and reported)
        assert len(Observability._reporter._queue) == 1

    def test_api_key_passed_to_reporter(self):
        """api_key should be stored in Reporter for Authorization header."""
        Observability.init(
            endpoint="http://localhost:99999",
            api_key="test-api-key-123",
        )
        assert Observability._reporter.api_key == "test-api-key-123"

    def test_traceparent_flags_when_not_sampled(self):
        """When sampled=False, traceparent flags should be 00."""
        from llm_observability.context import SpanContext
        ctx = SpanContext(
            trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
            span_kind=SpanKind.AGENT, sampled=False,
        )
        tp = inject_traceparent(ctx)
        # flags should be 00
        parts = tp.split("-")
        assert parts[3] == "00", f"Expected flags=00 for sampled=False, got {parts[3]}"

    def test_traceparent_flags_when_sampled(self):
        """When sampled=True, traceparent flags should be 01."""
        from llm_observability.context import SpanContext
        ctx = SpanContext(
            trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
            span_kind=SpanKind.AGENT, sampled=True,
        )
        tp = inject_traceparent(ctx)
        parts = tp.split("-")
        assert parts[3] == "01", f"Expected flags=01 for sampled=True, got {parts[3]}"


# ──────────────────────────────────────────────
# P1-3: SDK/Proxy Masking Consistency
# ──────────────────────────────────────────────

class TestP13MaskingConsistency:
    """P1-3: SDK masking rules should match Proxy masking rules."""

    def test_mask_openai_key_in_text(self):
        """sk-* patterns in text should be masked."""
        text = "my key is sk-abcdefghijklmnopqrstuvwxyz1234"
        masked = _mask_string_patterns(text)
        assert "sk-abcdefghij" not in masked
        assert "REDACTED" in masked

    def test_mask_bearer_token_in_text(self):
        """Bearer tokens in text should be masked."""
        text = "Authorization: Bearer abc123def456ghi789"
        masked = _mask_string_patterns(text)
        assert "abc123def456" not in masked
        assert "REDACTED" in masked

    def test_mask_password_in_text(self):
        """password=xxx patterns in text should be masked."""
        text = "password=secret123"
        masked = _mask_string_patterns(text)
        assert "secret123" not in masked
        assert "REDACTED" in masked

    def test_mask_token_in_text(self):
        """token=xxx patterns in text should be masked."""
        text = "token=abc123xyz"
        masked = _mask_string_patterns(text)
        assert "abc123xyz" not in masked
        assert "REDACTED" in masked

    def test_mask_secret_in_text(self):
        """secret=xxx patterns in text should be masked."""
        text = "secret=mysecret"
        masked = _mask_string_patterns(text)
        assert "mysecret" not in masked
        assert "REDACTED" in masked

    def test_mask_sensitive_keys_in_dict(self):
        """Sensitive keys in dict should be redacted."""
        data = {
            "api_key": "sk-xxx",
            "password": "secret",
            "token": "abc",
            "normal_field": "keep this",
        }
        masked = mask_payload(data, "masked")
        assert masked["api_key"] == "***REDACTED***"
        assert masked["password"] == "***REDACTED***"
        assert masked["token"] == "***REDACTED***"
        assert masked["normal_field"] == "keep this"

    def test_mask_cookie_in_dict(self):
        """Cookie should be redacted."""
        data = {"cookie": "session=abc123", "set-cookie": "token=xyz"}
        masked = mask_payload(data, "masked")
        assert masked["cookie"] == "***REDACTED***"
        assert masked["set-cookie"] == "***REDACTED***"

    def test_proxy_config_has_same_sensitive_keys(self):
        """Proxy config should have equivalent sensitive headers."""
        config = ProxyConfig()
        sensitive_lower = set(k.lower() for k in config._default_mask_keys)
        # Must include these keys
        for key in ["authorization", "api_key", "password", "token", "secret", "cookie"]:
            found = any(key in k for k in sensitive_lower)
            assert found, f"Proxy config missing sensitive key: {key}"


# ──────────────────────────────────────────────
# P1-4: Dedup Propagation
# ──────────────────────────────────────────────

class TestP14DedupPropagation:
    """P1-4: Dedup must still inject traceparent + ownership marker."""

    def test_dedup_injects_traceparent(self):
        """When logical_llm_span_active=True, traceparent should still be injected."""
        # Simulate: create an LLM context with dedup flag
        ctx = SpanContext(
            trace_id="a" * 32, span_id="b" * 16, parent_span_id="c" * 16,
            span_kind=SpanKind.LLM, sampled=True,
            logical_llm_span_active=True,
        )
        headers = inject_headers(ctx, is_logical_llm=True)

        assert "traceparent" in headers
        assert headers["traceparent"].startswith("00-")
        assert "X-LLM-OBS-Span-Role" in headers
        assert headers["X-LLM-OBS-Span-Role"] == "llm"


# ──────────────────────────────────────────────
# P1-5: Internal Header Stripping
# ──────────────────────────────────────────────

class TestP15InternalHeaderStripping:
    """P1-5: Internal observability headers must not leak to upstream."""

    def test_proxy_config_strips_internal_headers(self):
        """Proxy sensitive_headers should include internal observability headers."""
        config = ProxyConfig()
        sensitive = set(k.lower() for k in config.sensitive_headers)

        internal_headers = [
            "x-llm-obs-span-role",
            "x-session-id",
            "x-user-id",
            "x-app-name",
            "x-business-scene",
        ]
        for header in internal_headers:
            assert header in sensitive, \
                f"Internal header {header} not in proxy sensitive_headers"

    def test_build_forward_headers_strips_internal(self):
        """_build_forward_headers should strip internal observability headers."""
        from handler import ProxyHandler
        from reporter import TelemetryReporter

        config = ProxyConfig()
        config.upstream_url = "http://localhost:99999"
        reporter = TelemetryReporter(
            endpoint="http://localhost:99999",
        )
        handler = ProxyHandler(config, reporter)

        # Simulate a request with internal headers
        from aiohttp.test_utils import make_mocked_request
        request = make_mocked_request(
            "POST", "/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer sk-test",
                "X-LLM-OBS-Span-Role": "llm",
                "X-Session-Id": "sess-123",
                "X-User-Id": "user-456",
                "X-App-Name": "my-app",
                "X-Business-Scene": "testing",
                "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
            },
        )

        forward_headers = handler._build_forward_headers(request)

        # Internal headers must NOT be in forward headers
        for key in forward_headers:
            kl = key.lower()
            assert kl != "x-llm-obs-span-role", "Internal header leaked!"
            assert kl != "x-session-id", "Internal header leaked!"
            assert kl != "x-user-id", "Internal header leaked!"
            assert kl != "x-app-name", "Internal header leaked!"
            assert kl != "x-business-scene", "Internal header leaked!"

        # traceparent should also be stripped (will be re-injected by caller)
        for key in forward_headers:
            assert key.lower() != "traceparent", "traceparent should be stripped"

        # Authorization should be preserved (for upstream auth)
        auth_found = any(k.lower() == "authorization" for k in forward_headers)
        assert auth_found, "Authorization should be preserved for upstream"
