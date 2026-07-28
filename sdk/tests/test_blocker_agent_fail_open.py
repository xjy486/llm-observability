"""Remaining Blocker: AGENT Root fail-open tests.

Verifies that instrumentation finalization errors never affect business
results or original exceptions:
- span.end() failure preserves success result
- span.end() failure preserves business exception
- set_error() failure preserves business exception
- to_record() failure preserves success result
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
import unittest.mock as mock
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import get_current_context, _context_var
from llm_observability.integrations.langchain.agent_wrapper import observe_agent


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    if _context_var.get() is not None:
        _context_var.set(None)
    Observability.init(app_name="fail-open-test", endpoint="http://localhost:99999")
    yield Observability._tracer
    Observability.shutdown()


class BusinessError(Exception):
    pass


def test_agent_span_end_failure_preserves_success_result(init_sdk):
    """Business succeeds, span.end() fails → business result returned, no exception."""
    tracer = init_sdk

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(return_value={"answer": 42})

    observed = observe_agent(fake_agent, name="end-fail-success", root_mode="create")

    with mock.patch("llm_observability.spans.Span.end", side_effect=RuntimeError("end crashed")):
        result = observed.invoke({"messages": []})

    # Business result MUST be preserved — instrumentation error swallowed
    assert result == {"answer": 42}, (
        f"Business result lost due to span.end() failure: {result}"
    )

    # Context must be restored
    assert get_current_context() is None


def test_agent_span_end_failure_preserves_business_exception(init_sdk):
    """Business raises ValueError, span.end() also fails → ValueError propagates, not RuntimeError."""
    tracer = init_sdk

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(side_effect=ValueError("business logic error"))

    observed = observe_agent(fake_agent, name="end-fail-exc", root_mode="create")

    with mock.patch("llm_observability.spans.Span.end", side_effect=RuntimeError("end crashed")):
        with pytest.raises(ValueError) as exc_info:
            observed.invoke({"messages": []})

    # Original business exception preserved, NOT replaced by RuntimeError
    assert isinstance(exc_info.value, ValueError), (
        f"Expected ValueError, got {type(exc_info.value).__name__} — "
        "instrumentation error replaced business exception"
    )
    assert "business logic error" in str(exc_info.value)

    # Context must be restored
    assert get_current_context() is None


def test_agent_set_error_failure_preserves_business_exception(init_sdk):
    """Business raises ValueError, set_error() also fails → ValueError propagates."""
    tracer = init_sdk

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(side_effect=ValueError("original error"))

    observed = observe_agent(fake_agent, name="set-error-fail", root_mode="create")

    with mock.patch("llm_observability.spans.Span.set_error", side_effect=RuntimeError("set_error crashed")):
        with pytest.raises(ValueError) as exc_info:
            observed.invoke({"messages": []})

    # Original exception preserved
    assert isinstance(exc_info.value, ValueError), (
        f"Expected ValueError, got {type(exc_info.value).__name__}"
    )
    assert "original error" in str(exc_info.value)

    # Context must be restored
    assert get_current_context() is None


def test_agent_to_record_failure_preserves_success_result(init_sdk):
    """Business succeeds, to_record() fails → business result returned, no exception."""
    tracer = init_sdk

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(return_value={"output": "success"})

    observed = observe_agent(fake_agent, name="to-record-fail", root_mode="create")

    with mock.patch("llm_observability.spans.Span.to_record", side_effect=RuntimeError("to_record crashed")):
        result = observed.invoke({"messages": []})

    # Business result preserved
    assert result == {"output": "success"}, (
        f"Business result lost due to to_record() failure: {result}"
    )

    # Context must be restored
    assert get_current_context() is None
