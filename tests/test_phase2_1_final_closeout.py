"""
Phase 2.1 Final Closeout tests — P0 and P1 fix verification.

Covers:
  P0-1: Streaming ContextVar decoupled from Span lifetime
  P0-2: Sampling inherited across full trace
  P1-1: Reporter shutdown drains full queue
  P1-2: No-SDK trace metadata fallback
  P1-3: Session/User metrics trace-level filter
  P1-4: Unified masking key set

Run:  pytest tests/test_phase2_1_final_closeout.py -v
"""
import sys
import os
import time
import json
import asyncio

# Path setup
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "core"))
sys.path.insert(0, os.path.join(ROOT, "proxy"))

import pytest
from llm_observability.context import SpanContext, get_current_context, set_context, reset_context
from llm_observability.spans import Span, SpanKind
from llm_observability.instrumentation.openai import ObservedStream


# ──────────────────────────────────────────────
# P0-1: Streaming ContextVar Decoupled
# ──────────────────────────────────────────────

class TestP01StreamingContextDecoupled:
    """P0-1: ContextVar must be restored immediately after create(), not on stream finalize."""

    def _make_mock_stream(self, chunks=None, error=None):
        class MockChunk:
            def __init__(self, content):
                self.choices = [type("C", (), {"delta": type("D", (), {"content": content})()})()]
        class MockStream:
            def __init__(self):
                self._chunks = chunks or [MockChunk("hello")]
                self._idx = 0
                self._closed = False
            def __iter__(self): return self
            def __next__(self):
                if error: raise error
                if self._idx >= len(self._chunks): raise StopIteration
                c = self._chunks[self._idx]; self._idx += 1; return c
            def close(self): self._closed = True
        return MockStream()

    def _make_tracer_mock(self):
        class MockTracer:
            class MockConfig:
                payload_strategy = "masked"
            config = MockConfig()
            class MockReporter:
                reported = []
                def report(self, record): self.reported.append(record)
            reporter = MockReporter()
        return MockTracer()

    def test_context_restored_after_stream_create(self):
        """After ObservedStream is created, current context must be the parent, not LLM."""
        parent_ctx = SpanContext(
            trace_id="t1", span_id="parent", parent_span_id=None,
            span_kind=SpanKind.AGENT, sampled=True,
        )
        token = set_context(parent_ctx)

        span = Span(trace_id="t1", span_id="s1", parent_span_id="parent",
                    span_name="llm.completion", span_kind=SpanKind.LLM)
        span.start()
        tracer = self._make_tracer_mock()

        # ObservedStream no longer takes token — context is NOT changed by it
        observed = ObservedStream(self._make_mock_stream(), span, tracer, sampled=True)

        # Context must still be parent (ObservedStream doesn't touch context)
        current = get_current_context()
        assert current is not None
        assert current.span_id == "parent", \
            f"Context should be parent after create, got {current.span_id}"

        reset_context(token)

    def test_stream_not_holding_token(self):
        """ObservedStream must NOT have a _token attribute."""
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None,
                    span_name="llm", span_kind=SpanKind.LLM)
        span.start()
        tracer = self._make_tracer_mock()
        observed = ObservedStream(self._make_mock_stream(), span, tracer, sampled=True)
        assert not hasattr(observed, '_token') or observed.__dict__.get('_token') is None, \
            "ObservedStream must not hold a ContextVar token"

    def test_stream_exception_finalizes_span(self):
        """Stream error: span ERROR, context not held by stream."""
        parent_ctx = SpanContext(
            trace_id="t1", span_id="parent", parent_span_id=None,
            span_kind=SpanKind.AGENT, sampled=True,
        )
        token = set_context(parent_ctx)

        span = Span(trace_id="t1", span_id="s1", parent_span_id="parent",
                    span_name="llm.completion", span_kind=SpanKind.LLM)
        span.start()
        tracer = self._make_tracer_mock()

        observed = ObservedStream(
            self._make_mock_stream(error=ConnectionError("stream broke")),
            span, tracer, sampled=True,
        )

        with pytest.raises(ConnectionError):
            for _ in observed:
                pass

        assert observed._finalized is True
        assert span.status == "ERROR"
        # Context is still parent (was never changed by ObservedStream)
        current = get_current_context()
        assert current is not None
        assert current.span_id == "parent"

        reset_context(token)

    def test_stream_close_finalizes_without_context(self):
        """close() finalizes span; does not need context token."""
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None,
                    span_name="llm", span_kind=SpanKind.LLM)
        span.start()
        tracer = self._make_tracer_mock()
        observed = ObservedStream(self._make_mock_stream(), span, tracer, sampled=True)
        observed.close()
        assert observed._finalized is True
        assert span.status == "OK"


# ──────────────────────────────────────────────
# P0-2: Sampling Inherited
# ──────────────────────────────────────────────

class TestP02SamplingInherited:
    """P0-2: Sampling decision must propagate across full trace."""

    def test_llm_span_not_reported_when_not_sampled(self):
        """LLM span finalize must check sampled=False and skip report."""
        span = Span(trace_id="t1", span_id="s1", parent_span_id="p",
                    span_name="llm", span_kind=SpanKind.LLM)
        span.start()

        class MockTracer:
            class MockConfig: payload_strategy = "masked"
            config = MockConfig()
            class MockReporter:
                reported = []
                def report(self, r): self.reported.append(r)
            reporter = MockReporter()

        class MockStream:
            def __iter__(self): return self
            def __next__(self): raise StopIteration
            def close(self): pass

        tracer = MockTracer()
        observed = ObservedStream(MockStream(), span, tracer, sampled=False)
        for _ in observed:
            pass

        assert observed._finalized is True
        assert len(tracer.reporter.reported) == 0, "Should not report when sampled=False"

    def test_llm_span_reported_when_sampled(self):
        """LLM span finalize must report when sampled=True."""
        span = Span(trace_id="t1", span_id="s1", parent_span_id="p",
                    span_name="llm", span_kind=SpanKind.LLM)
        span.start()

        class MockTracer:
            class MockConfig: payload_strategy = "masked"
            config = MockConfig()
            class MockReporter:
                reported = []
                def report(self, r): self.reported.append(r)
            reporter = MockReporter()

        class MockStream:
            def __iter__(self): return self
            def __next__(self): raise StopIteration
            def close(self): pass

        tracer = MockTracer()
        observed = ObservedStream(MockStream(), span, tracer, sampled=True)
        for _ in observed:
            pass

        assert len(tracer.reporter.reported) == 1, "Should report when sampled=True"

    def test_traceparent_flags_zero_means_not_sampled_proxy(self):
        """Proxy must not report telemetry when traceparent flags=00."""
        from trace_context import parse_traceparent
        ctx = parse_traceparent("00-" + "a" * 32 + "-" + "b" * 16 + "-00")
        assert ctx is not None
        assert ctx.sampled is False, "flags=00 means not sampled"

    def test_traceparent_flags_one_means_sampled_proxy(self):
        """Proxy must report when traceparent flags=01."""
        from trace_context import parse_traceparent
        ctx = parse_traceparent("00-" + "a" * 32 + "-" + "b" * 16 + "-01")
        assert ctx is not None
        assert ctx.sampled is True

    def test_proxy_should_sample_inherits_not_sampled(self):
        """Proxy returns False when trace_ctx inherited and not sampled."""
        from handler import ProxyHandler
        from reporter import TelemetryReporter
        from trace_context import TraceContext
        from config import ProxyConfig

        config = ProxyConfig()
        config.sample_rate = 1.0
        reporter = TelemetryReporter(endpoint="http://localhost:99999")
        handler = ProxyHandler(config, reporter)

        ctx = TraceContext(
            trace_id="a" * 32, span_id="c" * 16, parent_span_id="b" * 16,
            trace_flags="00", inherited=True,
        )
        assert handler._should_sample_for_ctx(ctx, is_error=False) is False

    def test_proxy_should_sample_inherits_sampled(self):
        """Proxy returns True when trace_ctx inherited and sampled."""
        from handler import ProxyHandler
        from reporter import TelemetryReporter
        from trace_context import TraceContext
        from config import ProxyConfig

        config = ProxyConfig()
        reporter = TelemetryReporter(endpoint="http://localhost:99999")
        handler = ProxyHandler(config, reporter)

        ctx = TraceContext(
            trace_id="a" * 32, span_id="c" * 16, parent_span_id="b" * 16,
            trace_flags="01", inherited=True,
        )
        assert handler._should_sample_for_ctx(ctx, is_error=False) is True


# ──────────────────────────────────────────────
# P1-1: Reporter Shutdown Drain
# ──────────────────────────────────────────────

class TestP11ReporterDrainQueue:
    """P1-1: Reporter shutdown must drain entire queue, not just one batch."""

    def test_shutdown_drains_more_than_batch_size(self):
        """batch_size=10, queue=25 -> all 25 sent on shutdown."""
        from llm_observability.reporter import Reporter
        from unittest.mock import AsyncMock, MagicMock

        reporter = Reporter(
            endpoint="http://localhost:99999",
            batch_size=10,
            shutdown_timeout=5.0,
        )

        for i in range(25):
            reporter._queue.append({"trace_id": f"t{i}", "span_id": f"s{i}"})

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.close = AsyncMock()
        reporter._session = mock_session

        asyncio.run(reporter.stop())

        assert reporter._sent_count == 25, f"Expected 25 sent, got {reporter._sent_count}"
        assert len(reporter._queue) == 0, f"Queue should be empty, got {len(reporter._queue)}"

    def test_shutdown_timeout_does_not_hang(self):
        """If Core unavailable, shutdown returns within timeout."""
        from llm_observability.reporter import Reporter

        reporter = Reporter(
            endpoint="http://localhost:99999",
            batch_size=10,
            shutdown_timeout=1.0,
        )
        for i in range(5):
            reporter._queue.append({"t": i})

        reporter._session = None
        start = time.time()
        asyncio.run(reporter.stop())
        elapsed = time.time() - start
        assert elapsed < 3.0, f"Shutdown took {elapsed}s, should be fast"


# ──────────────────────────────────────────────
# P1-2: No-SDK Metadata Fallback
# ──────────────────────────────────────────────

class TestP12NoSDKMetadataFallback:
    """P1-2: No-SDK traces (LLM only, no AGENT) must have metadata in Summary."""

    def test_no_sdk_trace_has_metadata(self):
        """LLM-only trace: session_id/user_id/app_name from LLM span, not NULL."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        storage.insert_span({
            "trace_id": "t-noagent", "span_id": "llm-1",
            "parent_span_id": None,
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base, "end_time": base + 0.5,
            "duration_ms": 500, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S2", "user_id": "U2",
            "app_name": "App2", "business_scene": "Scene2",
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })

        result = storage.get_trace_summaries()
        assert result["total"] == 1
        trace = result["traces"][0]
        assert trace["session_id"] == "S2", f"Expected S2, got {trace['session_id']}"
        assert trace["user_id"] == "U2"
        assert trace["app_name"] == "App2"
        assert trace["business_scene"] == "Scene2"

    def test_sdk_trace_still_prefers_agent(self):
        """SDK trace: AGENT metadata preferred over LLM."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        storage.insert_span({
            "trace_id": "t-agent", "span_id": "agent-1",
            "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base, "end_time": base + 1.0,
            "duration_ms": 1000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S1", "user_id": "U1",
            "app_name": "App1", "business_scene": "Scene1",
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "t-agent", "span_id": "llm-1",
            "parent_span_id": "agent-1",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 0.1, "end_time": base + 0.5,
            "duration_ms": 400, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })

        result = storage.get_trace_summaries()
        trace = result["traces"][0]
        assert trace["session_id"] == "S1"
        assert trace["app_name"] == "App1"


# ──────────────────────────────────────────────
# P1-3: Session/User Metrics Trace-Level Filter
# ──────────────────────────────────────────────

class TestP13MetricsTraceLevelFilter:
    """P1-3: session_id/user_id filter must be trace-level, not span-level."""

    def test_session_filter_counts_child_llm(self):
        """AGENT session=S1, LLM session=NULL -> metrics should count the LLM span."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        storage.insert_span({
            "trace_id": "t1", "span_id": "agent", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base, "end_time": base + 1.0,
            "duration_ms": 1000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S1", "user_id": "U1",
            "app_name": "App", "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "t1", "span_id": "llm-1", "parent_span_id": "agent",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 0.1, "end_time": base + 0.5,
            "duration_ms": 400, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {
                "gen_ai.request.model": "gpt-4",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 20,
                "gen_ai.usage.total_tokens": 30,
            },
            "events": [], "payload": None, "request_metadata": None,
        })

        metrics = storage.get_metrics(session_id="S1")
        assert metrics["trace_count"] == 1, f"Expected 1 trace, got {metrics['trace_count']}"
        assert metrics["llm_call_count"] == 1, f"Expected 1 LLM call, got {metrics['llm_call_count']}"
        assert metrics["total_tokens"] == 30, f"Expected 30 tokens, got {metrics['total_tokens']}"

    def test_session_filter_no_match(self):
        """session_id that doesn't exist -> 0 everything."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60
        storage.insert_span({
            "trace_id": "t1", "span_id": "s1", "parent_span_id": None,
            "span_kind": "LLM", "span_name": "llm",
            "start_time": base, "end_time": base + 0.1,
            "duration_ms": 100, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S1", "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })

        metrics = storage.get_metrics(session_id="NONEXISTENT")
        assert metrics["trace_count"] == 0
        assert metrics["llm_call_count"] == 0


# ──────────────────────────────────────────────
# P1-4: Unified Masking Key Set
# ──────────────────────────────────────────────

class TestP14UnifiedMaskingKeys:
    """P1-4: SDK and Proxy must share the same sensitive key set."""

    def test_sdk_masks_access_token(self):
        from llm_observability.utils.masking import mask_payload
        data = {"access_token": "abc123"}
        masked = mask_payload(data, "masked")
        assert masked["access_token"] == "***REDACTED***"

    def test_sdk_masks_refresh_token(self):
        from llm_observability.utils.masking import mask_payload
        data = {"refresh_token": "def456"}
        masked = mask_payload(data, "masked")
        assert masked["refresh_token"] == "***REDACTED***"

    def test_sdk_masks_private_key(self):
        from llm_observability.utils.masking import mask_payload
        data = {"private_key": "ghi789"}
        masked = mask_payload(data, "masked")
        assert masked["private_key"] == "***REDACTED***"

    def test_sdk_masks_secret_key(self):
        from llm_observability.utils.masking import mask_payload
        data = {"secret_key": "jkl012"}
        masked = mask_payload(data, "masked")
        assert masked["secret_key"] == "***REDACTED***"

    def test_sdk_masks_x_api_key(self):
        from llm_observability.utils.masking import mask_payload
        data = {"x-api-key": "mno345"}
        masked = mask_payload(data, "masked")
        assert masked["x-api-key"] == "***REDACTED***"

    def test_sdk_masks_api_key_with_hyphen(self):
        from llm_observability.utils.masking import mask_payload
        data = {"api-key": "pqr678"}
        masked = mask_payload(data, "masked")
        assert masked["api-key"] == "***REDACTED***"

    def test_sdk_masks_proxy_authorization(self):
        from llm_observability.utils.masking import mask_payload
        data = {"proxy-authorization": "bearer xyz"}
        masked = mask_payload(data, "masked")
        assert masked["proxy-authorization"] == "***REDACTED***"

    def test_proxy_masks_access_token(self):
        from config import ProxyConfig
        from payload import mask_object
        config = ProxyConfig()
        data = {"access_token": "abc123"}
        masked = mask_object(data, config.mask_patterns, config.mask_keys)
        assert masked["access_token"] == "[REDACTED]"

    def test_proxy_masks_private_key(self):
        from config import ProxyConfig
        from payload import mask_object
        config = ProxyConfig()
        data = {"private_key": "ghi"}
        masked = mask_object(data, config.mask_patterns, config.mask_keys)
        assert masked["private_key"] == "[REDACTED]"

    def test_proxy_masks_x_api_key(self):
        from config import ProxyConfig
        from payload import mask_object
        config = ProxyConfig()
        data = {"x-api-key": "mno"}
        masked = mask_object(data, config.mask_patterns, config.mask_keys)
        assert masked["x-api-key"] == "[REDACTED]"

    def test_sdk_proxy_key_sets_match(self):
        """SDK and Proxy must have the same canonical key set."""
        from llm_observability.utils.privacy_constants import SENSITIVE_KEYS
        from config import ProxyConfig
        config = ProxyConfig()
        proxy_keys = set(k.lower() for k in config.mask_keys)
        sdk_keys = set(k.lower() for k in SENSITIVE_KEYS)
        assert proxy_keys == sdk_keys, f"Difference: {proxy_keys.symmetric_difference(sdk_keys)}"

    def test_text_patterns_all_masked(self):
        """Text with sk-*, Bearer, password=, token=, secret=, api_key= all masked."""
        from llm_observability.utils.masking import _mask_string_patterns
        texts = [
            "my key is sk-abcdefghijklmnopqrstuvwxyz1234",
            "Authorization: Bearer abc123def456",
            "password=secret123",
            "token=abc123xyz",
            "secret=mysecret",
            "api_key=sk-test123",
        ]
        for text in texts:
            masked = _mask_string_patterns(text)
            assert "REDACTED" in masked, f"Failed to mask: {text} -> {masked}"
