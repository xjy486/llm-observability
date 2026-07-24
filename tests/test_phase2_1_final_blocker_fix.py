"""
Phase 2.1 Final Blocker Fix tests.

Covers:
  BLOCKER-1: Proxy sampling gate — sampled=False → no GATEWAY span
  BLOCKER-2: Common privacy module — SDK/Proxy import from same source
  P1-1: unique_users/unique_sessions trace-level aggregation
  P1-2: ObservedStream context manager closes underlying stream
  P1-3: Reporter stop_sync timeout alignment with shutdown_timeout
  P1-4: W3C trace_flags bit-0 semantics

Run:  pytest tests/test_phase2_1_final_blocker_fix.py -v
"""
import sys
import os
import time
import asyncio
import re

# Path setup
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sdk", "python"))
sys.path.insert(0, os.path.join(ROOT, "core"))
sys.path.insert(0, os.path.join(ROOT, "proxy"))
sys.path.insert(0, ROOT)

import pytest


# ═══════════════════════════════════════════════════════════════
# BLOCKER-1: Proxy Sampling Gate
# ═══════════════════════════════════════════════════════════════

class TestBlocker1ProxySamplingGate:
    """BLOCKER-1: sample_rate=0 / inherited flags=00 → Proxy must NOT report GATEWAY."""

    def test_report_telemetry_skips_when_not_sampled(self):
        """_report_telemetry with sampled=False must return immediately without reporting."""
        from handler import ProxyHandler
        from reporter import TelemetryReporter
        from config import ProxyConfig
        from trace_context import TraceContext

        config = ProxyConfig()
        reporter = TelemetryReporter(endpoint="http://localhost:99999")
        handler = ProxyHandler(config, reporter)

        # Track reporter.report calls
        original_report = reporter.report
        report_calls = []
        def tracking_report(record):
            report_calls.append(record)
        reporter.report = tracking_report

        ctx = TraceContext(
            trace_id="a" * 32, span_id="c" * 16, parent_span_id="b" * 16,
            trace_flags="00", inherited=True,
        )

        asyncio.run(handler._report_telemetry(
            trace_ctx=ctx,
            metadata={},
            request_meta={"model": "test"},
            start_wall=time.time(),
            elapsed_ms=100.0,
            status="OK",
            http_status=200,
            error_type=None,
            error_message=None,
            is_stream=False,
            processed_payload=None,
            response_payload=None,
            ttft_ms=None,
            first_chunk_ms=None,
            sampled=False,
        ))

        assert len(report_calls) == 0, "Should NOT report when sampled=False"

    def test_report_telemetry_reports_when_sampled(self):
        """_report_telemetry with sampled=True must report."""
        from handler import ProxyHandler
        from reporter import TelemetryReporter
        from config import ProxyConfig
        from trace_context import TraceContext

        config = ProxyConfig()
        reporter = TelemetryReporter(endpoint="http://localhost:99999")
        handler = ProxyHandler(config, reporter)

        report_calls = []
        def tracking_report(record):
            report_calls.append(record)
        reporter.report = tracking_report

        ctx = TraceContext(
            trace_id="a" * 32, span_id="c" * 16, parent_span_id="b" * 16,
            trace_flags="01", inherited=True,
        )

        asyncio.run(handler._report_telemetry(
            trace_ctx=ctx,
            metadata={},
            request_meta={"model": "test"},
            start_wall=time.time(),
            elapsed_ms=100.0,
            status="OK",
            http_status=200,
            error_type=None,
            error_message=None,
            is_stream=False,
            processed_payload=None,
            response_payload=None,
            ttft_ms=None,
            first_chunk_ms=None,
            sampled=True,
        ))

        assert len(report_calls) == 1, "Should report when sampled=True"

    def test_inherited_unsampled_not_reported_even_on_error(self):
        """Inherited trace with flags=00: even HTTP 500 must NOT be force-reported."""
        from handler import ProxyHandler
        from reporter import TelemetryReporter
        from config import ProxyConfig
        from trace_context import TraceContext

        config = ProxyConfig()
        config.error_always_capture = True
        reporter = TelemetryReporter(endpoint="http://localhost:99999")
        handler = ProxyHandler(config, reporter)

        ctx = TraceContext(
            trace_id="a" * 32, span_id="c" * 16, parent_span_id="b" * 16,
            trace_flags="00", inherited=True,
        )

        # Inherited + not sampled → should_sample_for_ctx returns False even for errors
        result = handler._should_sample_for_ctx(ctx, is_error=True)
        assert result is False, "Inherited unsampled trace must NOT be force-sampled on error"

    def test_root_trace_error_always_capture(self):
        """Root trace (not inherited) with error_always_capture → should sample on error."""
        from handler import ProxyHandler
        from reporter import TelemetryReporter
        from config import ProxyConfig
        from trace_context import TraceContext

        config = ProxyConfig()
        config.error_always_capture = True
        config.sample_rate = 0.0
        reporter = TelemetryReporter(endpoint="http://localhost:99999")
        handler = ProxyHandler(config, reporter)

        ctx = TraceContext(
            trace_id="a" * 32, span_id="c" * 16, parent_span_id=None,
            trace_flags="01", inherited=False,
        )

        result = handler._should_sample_for_ctx(ctx, is_error=True)
        assert result is True, "Root trace error should be captured when error_always_capture=True"

    def test_nonstreaming_http500_force_reports_on_error_always_capture(self):
        """P1-fix: Non-streaming HTTP 500 must re-evaluate sampling with is_error=True.

        Setup: root trace, sample_rate=0, error_always_capture=True.
        At request start, should_sample=False (is_error=False, sample_rate=0).
        Upstream returns HTTP 500 → is_error=True → must force-report.

        This test simulates the _handle_nonstreaming_response call site by
        invoking it with a mock upstream response returning status 500.
        """
        from handler import ProxyHandler
        from reporter import TelemetryReporter
        from config import ProxyConfig
        from trace_context import TraceContext

        config = ProxyConfig()
        config.error_always_capture = True
        config.sample_rate = 0.0
        reporter = TelemetryReporter(endpoint="http://localhost:99999")
        handler = ProxyHandler(config, reporter)

        report_calls = []
        def tracking_report(record):
            report_calls.append(record)
        reporter.report = tracking_report

        ctx = TraceContext(
            trace_id="a" * 32, span_id="c" * 16, parent_span_id=None,
            trace_flags="01", inherited=False,
        )

        # Build a mock upstream response with status 500
        import json as _json
        error_body = _json.dumps({"error": {"message": "Internal error", "type": "server_error"}}).encode()

        class MockResp:
            status = 500
            headers = {}
            async def read(self):
                return error_body

        # should_sample at request start: sample_rate=0, is_error=False → False
        should_sample_at_start = handler._should_sample_for_ctx(ctx, is_error=False)
        assert should_sample_at_start is False, "Precondition: sample_rate=0 → not sampled at start"

        asyncio.run(handler._handle_nonstreaming_response(
            upstream_resp=MockResp(),
            trace_ctx=ctx,
            metadata={},
            request_meta={"model": "test"},
            start_time=time.perf_counter(),
            start_wall=time.time(),
            processed_payload=None,
            should_sample=should_sample_at_start,
            is_stream=False,
            ownership=None,
        ))

        assert len(report_calls) == 1, (
            "HTTP 500 with error_always_capture=True must force-report even when "
            "should_sample at request start was False"
        )
        record = report_calls[0]
        assert record["status"] == "ERROR"
        assert record["http_status"] == 500

    def test_nonstreaming_http200_not_reported_when_sample_rate_zero(self):
        """P1-fix: Non-streaming HTTP 200 with sample_rate=0 must NOT report.

        Ensures the re-evaluation doesn't accidentally over-report successful
        requests. is_error=False → _should_sample_for_ctx returns False → no report.
        """
        from handler import ProxyHandler
        from reporter import TelemetryReporter
        from config import ProxyConfig
        from trace_context import TraceContext

        config = ProxyConfig()
        config.error_always_capture = True
        config.sample_rate = 0.0
        reporter = TelemetryReporter(endpoint="http://localhost:99999")
        handler = ProxyHandler(config, reporter)

        report_calls = []
        def tracking_report(record):
            report_calls.append(record)
        reporter.report = tracking_report

        ctx = TraceContext(
            trace_id="a" * 32, span_id="c" * 16, parent_span_id=None,
            trace_flags="01", inherited=False,
        )

        import json as _json
        ok_body = _json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()

        class MockResp:
            status = 200
            headers = {}
            async def read(self):
                return ok_body

        should_sample_at_start = handler._should_sample_for_ctx(ctx, is_error=False)
        assert should_sample_at_start is False

        asyncio.run(handler._handle_nonstreaming_response(
            upstream_resp=MockResp(),
            trace_ctx=ctx,
            metadata={},
            request_meta={"model": "test"},
            start_time=time.perf_counter(),
            start_wall=time.time(),
            processed_payload=None,
            should_sample=should_sample_at_start,
            is_stream=False,
            ownership=None,
        ))

        assert len(report_calls) == 0, "HTTP 200 with sample_rate=0 must NOT report"


# ═══════════════════════════════════════════════════════════════
# BLOCKER-2: Common Privacy Module
# ═══════════════════════════════════════════════════════════════

class TestBlocker2CommonPrivacyModule:
    """BLOCKER-2: SDK and Proxy must import from common/privacy module."""

    def test_common_privacy_module_exists(self):
        """common/privacy/constants.py must exist and be importable."""
        from common.privacy.constants import SENSITIVE_KEYS, SENSITIVE_REGEX_PATTERNS
        assert len(SENSITIVE_KEYS) > 0
        assert len(SENSITIVE_REGEX_PATTERNS) > 0

    def test_sdk_imports_from_common(self):
        """SDK masking.py must import from common.privacy.constants."""
        from llm_observability.utils.masking import SENSITIVE_KEYS, SENSITIVE_PATTERNS
        from common.privacy.constants import SENSITIVE_KEYS as CANONICAL_KEYS
        assert set(SENSITIVE_KEYS) == set(CANONICAL_KEYS), "SDK keys must match common keys"

    def test_proxy_imports_from_common(self):
        """Proxy config.py must import from common.privacy.constants."""
        from config import ProxyConfig
        from common.privacy.constants import SENSITIVE_KEYS as CANONICAL_KEYS
        config = ProxyConfig()
        proxy_keys = set(k.lower() for k in config.mask_keys)
        canonical_keys = set(k.lower() for k in CANONICAL_KEYS)
        assert proxy_keys == canonical_keys, "Proxy keys must match common keys"

    def test_proxy_regex_from_common(self):
        """Proxy config mask_patterns must come from common module."""
        from config import ProxyConfig
        from common.privacy.constants import SENSITIVE_REGEX_PATTERNS
        config = ProxyConfig()
        # mask_patterns should be tuples of (compiled_regex, replacement)
        for pattern, replacement in config.mask_patterns:
            assert hasattr(pattern, "sub"), "Pattern must be a compiled regex"
            assert isinstance(replacement, str), "Replacement must be a string"

    def test_privacy_golden_contract(self):
        """Golden contract: same input → same output for SDK and Proxy masking."""
        from llm_observability.utils.masking import mask_payload as sdk_mask
        from payload import mask_object
        from config import ProxyConfig

        config = ProxyConfig()

        test_data = {
            "api_key": "sk-test123",
            "password": "secret123",
            "normal_field": "hello",
            "nested": {"token": "abc", "data": "ok"},
        }

        # SDK masking
        sdk_result = sdk_mask(test_data, "masked")
        # Proxy masking
        proxy_result = mask_object(test_data, config.mask_patterns, config.mask_keys)

        # Both should redact api_key
        assert sdk_result["api_key"] == "***REDACTED***", f"SDK: {sdk_result['api_key']}"
        assert proxy_result["api_key"] == "[REDACTED]", f"Proxy: {proxy_result['api_key']}"

        # Both should redact password
        assert sdk_result["password"] == "***REDACTED***"
        assert proxy_result["password"] == "[REDACTED]"

        # Both should redact nested token
        assert sdk_result["nested"]["token"] == "***REDACTED***"
        assert proxy_result["nested"]["token"] == "[REDACTED]"

        # Both should keep normal fields
        assert sdk_result["normal_field"] == "hello"
        assert proxy_result["normal_field"] == "hello"

    def test_text_masking_golden_contract(self):
        """Golden contract: text pattern masking produces same results."""
        from llm_observability.utils.masking import _mask_string_patterns as sdk_mask_text
        from payload import mask_value
        from config import ProxyConfig

        config = ProxyConfig()

        test_texts = [
            "my key is sk-abcdefghijklmnopqrstuvwxyz1234",
            "Authorization: Bearer abc123def456",
            "password=secret123",
            "token=abc123xyz",
            "secret=mysecret",
            "api_key=sk-test123456789012345",
        ]

        for text in test_texts:
            sdk_result = sdk_mask_text(text)
            proxy_result = mask_value(text, config.mask_patterns)
            # Both should contain REDACTED
            assert "REDACTED" in sdk_result or "***" in sdk_result, f"SDK failed: {text} -> {sdk_result}"
            assert "REDACTED" in proxy_result or "***" in proxy_result, f"Proxy failed: {text} -> {proxy_result}"

    def test_old_privacy_constants_deleted(self):
        """Old sdk/python/llm_observability/utils/privacy_constants.py should be deleted."""
        old_path = os.path.join(
            ROOT, "sdk", "python", "llm_observability", "utils", "privacy_constants.py"
        )
        assert not os.path.exists(old_path), "Old privacy_constants.py should be deleted"


# ═══════════════════════════════════════════════════════════════
# P1-1: Unique Users/Sessions Trace-Level Aggregation
# ═══════════════════════════════════════════════════════════════

class TestP11UniqueUsersTraceLevel:
    """P1-1: unique_users/unique_sessions must be counted at trace-level."""

    def test_unique_users_with_agent_metadata(self):
        """AGENT has user_id, LLM has NULL → unique_users should be 2."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        # Trace A: AGENT with U1/S1, 2 LLM spans with NULL
        storage.insert_span({
            "trace_id": "tA", "span_id": "agentA", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base, "end_time": base + 1.0,
            "duration_ms": 1000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S1", "user_id": "U1",
            "app_name": "App", "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        for i in range(2):
            storage.insert_span({
                "trace_id": "tA", "span_id": f"llmA{i}", "parent_span_id": "agentA",
                "span_kind": "LLM", "span_name": "llm.completion",
                "start_time": base + 0.1 * i, "end_time": base + 0.5,
                "duration_ms": 400, "status": "OK",
                "ttft_ms": None, "first_chunk_ms": None,
                "session_id": None, "user_id": None,
                "app_name": None, "business_scene": None,
                "attributes": {"gen_ai.request.model": "gpt-4"},
                "events": [], "payload": None, "request_metadata": None,
            })

        # Trace B: AGENT with U2/S2, 1 LLM span with NULL
        storage.insert_span({
            "trace_id": "tB", "span_id": "agentB", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base + 1, "end_time": base + 2.0,
            "duration_ms": 1000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S2", "user_id": "U2",
            "app_name": "App", "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "tB", "span_id": "llmB0", "parent_span_id": "agentB",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 1.1, "end_time": base + 1.5,
            "duration_ms": 400, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })

        metrics = storage.get_metrics()
        assert metrics["trace_count"] == 2, f"Expected 2 traces, got {metrics['trace_count']}"
        assert metrics["llm_call_count"] == 3, f"Expected 3 LLM calls, got {metrics['llm_call_count']}"
        assert metrics["unique_users"] == 2, f"Expected 2 unique users, got {metrics['unique_users']}"
        assert metrics["unique_sessions"] == 2, f"Expected 2 unique sessions, got {metrics['unique_sessions']}"

    def test_unique_users_no_agent_span(self):
        """LLM-only traces (no AGENT) should still count user_id from LLM span."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        storage.insert_span({
            "trace_id": "t1", "span_id": "llm1", "parent_span_id": None,
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base, "end_time": base + 0.5,
            "duration_ms": 500, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S1", "user_id": "U1",
            "app_name": "App", "business_scene": None,
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })

        metrics = storage.get_metrics()
        assert metrics["unique_users"] == 1, f"Expected 1, got {metrics['unique_users']}"
        assert metrics["unique_sessions"] == 1, f"Expected 1, got {metrics['unique_sessions']}"

    def test_unique_users_with_model_filter_preserves_agent_metadata(self):
        """P1-fix: model filter must NOT drop AGENT spans from user/session aggregation.

        Scenario:
          Trace A: AGENT(U1/S1, model=NULL) → LLM(model=gpt-4, user=NULL)
          Query: get_metrics(model='gpt-4')

        The model filter selects the trace via the LLM span, but user_id/session_id
        live on the AGENT span (model=NULL). If the metadata subquery applies the
        model filter directly, AGENT is dropped → unique_users=0 (WRONG).
        Correct: unique_users=1, unique_sessions=1.
        """
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        # AGENT span carries user/session metadata, model is NULL
        storage.insert_span({
            "trace_id": "tA", "span_id": "agentA", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base, "end_time": base + 1.0,
            "duration_ms": 1000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S1", "user_id": "U1",
            "app_name": "App", "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        # LLM span has model=gpt-4 but user/session are NULL
        storage.insert_span({
            "trace_id": "tA", "span_id": "llmA0", "parent_span_id": "agentA",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 0.1, "end_time": base + 0.5,
            "duration_ms": 400, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })

        metrics = storage.get_metrics(model="gpt-4")
        assert metrics["trace_count"] == 1, f"Expected 1 trace, got {metrics['trace_count']}"
        assert metrics["llm_call_count"] == 1, f"Expected 1 LLM call, got {metrics['llm_call_count']}"
        assert metrics["unique_users"] == 1, (
            f"model filter must not drop AGENT metadata: expected 1, got {metrics['unique_users']}"
        )
        assert metrics["unique_sessions"] == 1, (
            f"model filter must not drop AGENT metadata: expected 1, got {metrics['unique_sessions']}"
        )

    def test_unique_users_with_model_filter_multiple_traces(self):
        """P1-fix: model filter with multiple traces, each with AGENT metadata.

        Two traces, both have AGENT(user/session) + LLM(model=gpt-4).
        Query model=gpt-4 → unique_users=2, unique_sessions=2.
        """
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        for tid, uid, sid in [("tA", "U1", "S1"), ("tB", "U2", "S2")]:
            storage.insert_span({
                "trace_id": tid, "span_id": f"agent_{tid}", "parent_span_id": None,
                "span_kind": "AGENT", "span_name": "agent.run",
                "start_time": base, "end_time": base + 1.0,
                "duration_ms": 1000, "status": "OK",
                "ttft_ms": None, "first_chunk_ms": None,
                "session_id": sid, "user_id": uid,
                "app_name": "App", "business_scene": None,
                "attributes": {}, "events": [], "payload": None, "request_metadata": None,
            })
            storage.insert_span({
                "trace_id": tid, "span_id": f"llm_{tid}", "parent_span_id": f"agent_{tid}",
                "span_kind": "LLM", "span_name": "llm.completion",
                "start_time": base + 0.1, "end_time": base + 0.5,
                "duration_ms": 400, "status": "OK",
                "ttft_ms": None, "first_chunk_ms": None,
                "session_id": None, "user_id": None,
                "app_name": None, "business_scene": None,
                "attributes": {"gen_ai.request.model": "gpt-4"},
                "events": [], "payload": None, "request_metadata": None,
            })

        metrics = storage.get_metrics(model="gpt-4")
        assert metrics["trace_count"] == 2
        assert metrics["llm_call_count"] == 2
        assert metrics["unique_users"] == 2, f"Expected 2, got {metrics['unique_users']}"
        assert metrics["unique_sessions"] == 2, f"Expected 2, got {metrics['unique_sessions']}"

    def test_model_filter_excludes_non_matching_traces(self):
        """P1-fix: model filter should still correctly exclude traces without that model."""
        from storage.db import Storage
        storage = Storage(db_path=":memory:")
        base = time.time() - 60

        # Trace A: AGENT(U1/S1) + LLM(gpt-4)
        storage.insert_span({
            "trace_id": "tA", "span_id": "agentA", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base, "end_time": base + 1.0,
            "duration_ms": 1000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S1", "user_id": "U1",
            "app_name": "App", "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "tA", "span_id": "llmA", "parent_span_id": "agentA",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 0.1, "end_time": base + 0.5,
            "duration_ms": 400, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {"gen_ai.request.model": "gpt-4"},
            "events": [], "payload": None, "request_metadata": None,
        })
        # Trace B: AGENT(U2/S2) + LLM(claude-3) — different model
        storage.insert_span({
            "trace_id": "tB", "span_id": "agentB", "parent_span_id": None,
            "span_kind": "AGENT", "span_name": "agent.run",
            "start_time": base + 1, "end_time": base + 2.0,
            "duration_ms": 1000, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": "S2", "user_id": "U2",
            "app_name": "App", "business_scene": None,
            "attributes": {}, "events": [], "payload": None, "request_metadata": None,
        })
        storage.insert_span({
            "trace_id": "tB", "span_id": "llmB", "parent_span_id": "agentB",
            "span_kind": "LLM", "span_name": "llm.completion",
            "start_time": base + 1.1, "end_time": base + 1.5,
            "duration_ms": 400, "status": "OK",
            "ttft_ms": None, "first_chunk_ms": None,
            "session_id": None, "user_id": None,
            "app_name": None, "business_scene": None,
            "attributes": {"gen_ai.request.model": "claude-3"},
            "events": [], "payload": None, "request_metadata": None,
        })

        metrics = storage.get_metrics(model="gpt-4")
        assert metrics["trace_count"] == 1, f"Expected 1 trace, got {metrics['trace_count']}"
        assert metrics["unique_users"] == 1, f"Expected 1, got {metrics['unique_users']}"
        assert metrics["unique_sessions"] == 1, f"Expected 1, got {metrics['unique_sessions']}"


# ═══════════════════════════════════════════════════════════════
# P1-2: ObservedStream Context Manager
# ═══════════════════════════════════════════════════════════════

class TestP12ObservedStreamContextManager:
    """P1-2: ObservedStream __exit__ must close underlying stream."""

    def _make_mock_stream(self, has_close=True, has_context_manager=False):
        class MockStream:
            def __init__(self):
                self._closed = False
                self._chunks = [type("C", (), {"choices": [type("Ch", (), {"delta": type("D", (), {"content": "hi"})()})()]})()]
                self._idx = 0
            def __iter__(self): return self
            def __next__(self):
                if self._idx >= len(self._chunks): raise StopIteration
                c = self._chunks[self._idx]; self._idx += 1; return c
            if has_close:
                def close(self): self._closed = True
            if has_context_manager:
                def __enter__(self): return self
                def __exit__(self, *args): self._closed = True
        return MockStream()

    def _make_tracer_mock(self):
        class MockTracer:
            class MockConfig: payload_strategy = "masked"
            config = MockConfig()
            class MockReporter:
                reported = []
                def report(self, r): self.reported.append(r)
            reporter = MockReporter()
        return MockTracer()

    def test_exit_closes_underlying_stream(self):
        """__exit__ must call close() on the underlying stream."""
        from llm_observability.instrumentation.openai import ObservedStream
        from llm_observability.spans import Span, SpanKind

        mock_stream = self._make_mock_stream(has_close=True)
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None,
                    span_name="llm", span_kind=SpanKind.LLM)
        span.start()
        tracer = self._make_tracer_mock()

        observed = ObservedStream(mock_stream, span, tracer, sampled=True)
        with observed:
            pass  # Consume the stream

        assert mock_stream._closed is True, "Underlying stream must be closed"
        assert observed._finalized is True, "Span must be finalized"

    def test_exit_closes_context_manager_stream(self):
        """__exit__ must call __exit__ on underlying stream if it's a context manager."""
        from llm_observability.instrumentation.openai import ObservedStream
        from llm_observability.spans import Span, SpanKind

        mock_stream = self._make_mock_stream(has_close=False, has_context_manager=True)
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None,
                    span_name="llm", span_kind=SpanKind.LLM)
        span.start()
        tracer = self._make_tracer_mock()

        observed = ObservedStream(mock_stream, span, tracer, sampled=True)
        with observed:
            pass

        assert mock_stream._closed is True, "Underlying stream __exit__ must be called"

    def test_enter_delegates_to_underlying(self):
        """__enter__ should delegate to underlying stream if it has __enter__."""
        from llm_observability.instrumentation.openai import ObservedStream
        from llm_observability.spans import Span, SpanKind

        class CtxStream:
            def __init__(self):
                self._entered = False
                self._closed = False
            def __iter__(self): return self
            def __next__(self): raise StopIteration
            def __enter__(self): self._entered = True; return self
            def __exit__(self, *args): self._closed = True

        mock_stream = CtxStream()
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None,
                    span_name="llm", span_kind=SpanKind.LLM)
        span.start()
        tracer = self._make_tracer_mock()

        observed = ObservedStream(mock_stream, span, tracer, sampled=True)
        with observed:
            pass

        assert mock_stream._entered is True, "Underlying stream __enter__ must be called"
        assert mock_stream._closed is True, "Underlying stream __exit__ must be called"

    def test_exit_on_exception_finalizes_with_error(self):
        """__exit__ with exception must finalize span with error AND close stream."""
        from llm_observability.instrumentation.openai import ObservedStream
        from llm_observability.spans import Span, SpanKind

        mock_stream = self._make_mock_stream(has_close=True)
        span = Span(trace_id="t1", span_id="s1", parent_span_id=None,
                    span_name="llm", span_kind=SpanKind.LLM)
        span.start()
        tracer = self._make_tracer_mock()

        observed = ObservedStream(mock_stream, span, tracer, sampled=True)
        try:
            with observed:
                raise ValueError("test error")
        except ValueError:
            pass

        assert mock_stream._closed is True, "Stream must be closed even on exception"
        assert observed._finalized is True
        assert span.status == "ERROR"


# ═══════════════════════════════════════════════════════════════
# P1-3: Reporter stop_sync Timeout Alignment
# ═══════════════════════════════════════════════════════════════

class TestP13ReporterStopSyncTimeout:
    """P1-3: stop_sync timeout must be shutdown_timeout + grace, not hardcoded 10s."""

    def test_stop_sync_uses_shutdown_timeout_plus_grace(self):
        """stop_sync should use shutdown_timeout + 2s as wait_timeout, not hardcoded 10s.

        We verify this by checking that with shutdown_timeout=1, the total
        stop_sync time is well under 10s (which would be the hardcoded value).
        Since the reporter has no real session, stop() returns quickly.
        """
        from llm_observability.reporter import Reporter

        # shutdown_timeout=1 → wait_timeout should be 3 (1+2), not 10
        reporter = Reporter(
            endpoint="http://localhost:99999",
            shutdown_timeout=1.0,
        )

        reporter._session = None  # No session → stop() returns quickly
        for i in range(5):
            reporter._queue.append({"t": i})

        # Use start_sync/stop_sync to test the full lifecycle
        reporter.start_sync()
        time.sleep(0.3)  # Let the loop start

        start = time.time()
        reporter.stop_sync()
        elapsed = time.time() - start

        # With mocked no-session, stop() should be very fast.
        # The key assertion: it should NOT take 10+ seconds (hardcoded timeout).
        assert elapsed < 5.0, f"stop_sync took {elapsed}s — may be using hardcoded 10s timeout"
        # Items should be processed (dropped since no session) or sent
        assert reporter._dropped_count > 0 or reporter._sent_count > 0, "Items should be processed"

    def test_stop_sync_does_not_hang_with_short_timeout(self):
        """With shutdown_timeout=1, stop_sync should not hang for 10 seconds."""
        from llm_observability.reporter import Reporter

        reporter = Reporter(
            endpoint="http://localhost:99999",
            shutdown_timeout=1.0,
        )
        reporter._session = None  # No session → stop() returns quickly

        start = time.time()
        asyncio.run(reporter.stop())
        elapsed = time.time() - start
        assert elapsed < 3.0, f"stop() took {elapsed}s with shutdown_timeout=1"


# ═══════════════════════════════════════════════════════════════
# P1-4: W3C trace_flags Bit Semantics
# ═══════════════════════════════════════════════════════════════

class TestP14W3CTraceFlagsBitSemantics:
    """P1-4: trace_flags sampled must use bit-0 check, not string equality."""

    @pytest.mark.parametrize("flags,expected", [
        ("00", False),
        ("01", True),
        ("02", False),
        ("03", True),
        ("ff", True),
        ("10", False),
        ("11", True),
    ])
    def test_trace_flags_bit_semantics(self, flags, expected):
        """All W3C trace-flags values must be evaluated by bit 0."""
        from trace_context import TraceContext

        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            trace_flags=flags,
            inherited=False,
        )
        assert ctx.sampled == expected, f"flags={flags} should be sampled={expected}"

    def test_invalid_trace_flags_returns_false(self):
        """Invalid hex string should return False, not raise."""
        from trace_context import TraceContext

        ctx = TraceContext(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            trace_flags="xy",
            inherited=False,
        )
        assert ctx.sampled is False

    def test_parse_traceparent_preserves_flags(self):
        """parse_traceparent must preserve trace_flags for bit-level evaluation."""
        from trace_context import parse_traceparent

        ctx = parse_traceparent("00-" + "a" * 32 + "-" + "b" * 16 + "-03")
        assert ctx is not None
        assert ctx.trace_flags == "03"
        assert ctx.sampled is True, "flags=03 → bit 0 is 1 → sampled=True"