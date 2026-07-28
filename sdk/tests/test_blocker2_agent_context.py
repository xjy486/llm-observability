"""Blocker 2: AGENT Root context restoration tests.

Verifies that TraceContextManager.__exit__ restores context even when
internal steps (str(exc), span.end, reporter) throw, and that the
original business exception is not replaced by instrumentation failures.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock
from llm_observability import Observability
from llm_observability.context import get_current_context, set_context, reset_context, SpanContext
from llm_observability.integrations.langchain.agent_wrapper import observe_agent


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    # Ensure clean context
    from llm_observability.context import _context_var
    if _context_var.get() is not None:
        _context_var.set(None)
    Observability.init(app_name="agent-ctx-test", endpoint="http://localhost:99999")
    yield Observability._tracer
    Observability.shutdown()


class BadStrException(Exception):
    """Exception whose __str__ raises — tests safe_error_message."""
    def __str__(self):
        raise RuntimeError("str() itself failed")


def test_agent_context_restored_when_exception_str_fails(init_sdk):
    """If str(exc_val) raises during error recording, AGENT context is restored."""
    tracer = init_sdk

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(side_effect=BadStrException())

    observed = observe_agent(fake_agent, name="bad-str-agent", root_mode="create")

    parent_ctx = get_current_context()
    with pytest.raises(BadStrException):
        observed.invoke({"messages": []})

    # Context MUST be restored to None (parent was None in create mode)
    current = get_current_context()
    assert current is None or current is parent_ctx, (
        f"AGENT context leaked after str(exc) failure — current={current}"
    )


def test_agent_business_exception_not_replaced_by_instrumentation_failure(init_sdk):
    """When str(exc) raises, the original business exception is re-raised, not RuntimeError."""
    tracer = init_sdk

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(side_effect=BadStrException())

    observed = observe_agent(fake_agent, name="exc-preserve-agent", root_mode="create")

    # The original BadStrException must propagate, NOT RuntimeError("str() itself failed")
    with pytest.raises(BadStrException) as exc_info:
        observed.invoke({"messages": []})

    # Verify it's the ORIGINAL exception, not a replacement
    assert isinstance(exc_info.value, BadStrException), (
        f"Expected BadStrException, got {type(exc_info.value).__name__}"
    )


def test_agent_context_restored_when_span_end_fails(init_sdk):
    """If span.end() raises, AGENT context is still restored."""
    tracer = init_sdk

    import unittest.mock as mock
    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(return_value={"result": "ok"})

    observed = observe_agent(fake_agent, name="end-fail-agent", root_mode="create")

    with mock.patch("llm_observability.spans.Span.end", side_effect=RuntimeError("end failed")):
        # This should still succeed because the fail-open executor catches instrumentation errors
        # OR it should raise but context must be restored
        try:
            result = observed.invoke({"messages": []})
        except RuntimeError:
            pass  # acceptable if it propagates, as long as context is restored

    current = get_current_context()
    assert current is None, (
        f"AGENT context leaked after span.end() failure — current={current}"
    )


def test_agent_context_restored_when_reporter_fails(init_sdk):
    """If reporter.report raises, AGENT context is still restored."""
    tracer = init_sdk

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(return_value={"result": "ok"})

    observed = observe_agent(fake_agent, name="reporter-fail-agent", root_mode="create")

    import unittest.mock as mock
    with mock.patch.object(tracer.reporter, "report", side_effect=RuntimeError("reporter dead")):
        result = observed.invoke({"messages": []})

    # Business result should be returned despite reporter failure
    assert result == {"result": "ok"}

    current = get_current_context()
    assert current is None, (
        f"AGENT context leaked after reporter failure — current={current}"
    )
