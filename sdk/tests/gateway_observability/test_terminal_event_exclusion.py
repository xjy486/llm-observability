"""P1-2: Terminal Event mutual-exclusion groups.

``GatewayEventRecorder`` records at most one terminal event per group on a
bound span: once one event in a group (``attempt`` / ``response`` / ``stream``)
is recorded, every other event in the same group is rejected. ``stream.*``
events are part of the terminal set (not just ``attempt``/``response``).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability.gateway_observability import PrivacyGuard
from llm_observability.gateway_observability.events import (
    EVENT_ATTEMPT_COMPLETED,
    EVENT_ATTEMPT_FAILED,
    EVENT_RESPONSE_COMPLETED,
    EVENT_RESPONSE_FAILED,
    EVENT_STREAM_COMPLETED,
    EVENT_STREAM_CANCELLED,
)
from llm_observability.gateway_observability.recorder import GatewayEventRecorder
from llm_observability.spans import Span, SpanKind


def _span():
    return Span(trace_id="a" * 32, span_id="1" * 16, parent_span_id=None,
                span_name="test", span_kind=SpanKind.GATEWAY)


def _events(span, name):
    return [e for e in span.events if e["name"] == name]


class TestTerminalEventExclusion:
    def test_attempt_completed_then_failed_rejected(self):
        rec = GatewayEventRecorder(span=_span(), privacy=PrivacyGuard(secret="s"))
        assert rec.attempt_completed(attempt_index=1, http_status_code=200) is True
        # The failed event in the same group is rejected.
        assert rec.attempt_failed(attempt_index=1, error_category="timeout") is False
        assert len(_events(rec.span, EVENT_ATTEMPT_COMPLETED)) == 1
        assert len(_events(rec.span, EVENT_ATTEMPT_FAILED)) == 0

    def test_attempt_failed_then_completed_rejected(self):
        rec = GatewayEventRecorder(span=_span(), privacy=PrivacyGuard(secret="s"))
        assert rec.attempt_failed(attempt_index=1, error_category="timeout") is True
        assert rec.attempt_completed(attempt_index=1, http_status_code=200) is False
        assert len(_events(rec.span, EVENT_ATTEMPT_FAILED)) == 1
        assert len(_events(rec.span, EVENT_ATTEMPT_COMPLETED)) == 0

    def test_response_completed_then_failed_rejected(self):
        rec = GatewayEventRecorder(span=_span(), privacy=PrivacyGuard(secret="s"))
        assert rec.response_completed(http_status_code=200) is True
        assert rec.response_failed(error_category="timeout") is False
        assert len(_events(rec.span, EVENT_RESPONSE_COMPLETED)) == 1
        assert len(_events(rec.span, EVENT_RESPONSE_FAILED)) == 0

    def test_stream_completed_then_cancelled_rejected(self):
        rec = GatewayEventRecorder(span=_span(), privacy=PrivacyGuard(secret="s"))
        assert rec.stream_completed() is True
        assert rec.stream_cancelled(error_category="client_cancelled") is False
        assert len(_events(rec.span, EVENT_STREAM_COMPLETED)) == 1
        assert len(_events(rec.span, EVENT_STREAM_CANCELLED)) == 0

    def test_stream_cancelled_then_completed_rejected(self):
        rec = GatewayEventRecorder(span=_span(), privacy=PrivacyGuard(secret="s"))
        assert rec.stream_cancelled(error_category="client_cancelled") is True
        assert rec.stream_completed() is False
        assert len(_events(rec.span, EVENT_STREAM_CANCELLED)) == 1
        assert len(_events(rec.span, EVENT_STREAM_COMPLETED)) == 0

    def test_response_failed_then_completed_rejected(self):
        rec = GatewayEventRecorder(span=_span(), privacy=PrivacyGuard(secret="s"))
        assert rec.response_failed(error_category="timeout") is True
        assert rec.response_completed(http_status_code=200) is False
        assert len(_events(rec.span, EVENT_RESPONSE_FAILED)) == 1
