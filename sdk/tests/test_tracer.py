"""Tests for the Tracer and Observability public API."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from llm_observability import Observability
from llm_observability.tracer import Tracer
from llm_observability.context import get_current_context


def _clean_init(**kwargs):
    """Ensure clean state before init."""
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(**kwargs)
    return Observability._tracer


def test_trace_creates_agent_root_span():
    """trace() creates an AGENT root span and sets it as current context."""
    tracer = _clean_init(
        app_name="test-app",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )

    with tracer.trace(name="my-task", session_id="s1", user_id="u1"):
        ctx = get_current_context()
        assert ctx is not None
        assert ctx.span_kind == "AGENT"
        assert ctx.parent_span_id is None  # root
        assert ctx.trace_id is not None

    # Context should be cleared after exiting
    assert get_current_context() is None

    Observability.shutdown()


def test_trace_records_span_on_exit():
    """trace() enqueues a span record to the reporter on exit."""
    tracer = _clean_init(
        app_name="test-app",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )

    with tracer.trace(name="my-task"):
        pass

    # Span should be in the reporter queue
    assert len(tracer.reporter._queue) == 1
    record = tracer.reporter._queue[0]
    assert record["span_name"] == "agent.run"
    assert record["span_kind"] == "AGENT"
    assert record["status"] == "OK"
    assert record["parent_span_id"] is None

    Observability.shutdown()


def test_trace_exception_sets_error_and_reraises():
    """trace() sets ERROR status on exception and re-raises."""
    tracer = _clean_init(
        app_name="test-app",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )

    raised = False
    try:
        with tracer.trace(name="failing-task"):
            raise RuntimeError("something went wrong")
    except RuntimeError:
        raised = True

    assert raised, "Exception should have been re-raised"
    assert len(tracer.reporter._queue) == 1
    record = tracer.reporter._queue[-1]
    assert record["status"] == "ERROR"
    assert record["error_type"] == "RuntimeError"
    assert "something went wrong" in record["error_message"]

    Observability.shutdown()


def test_trace_includes_metadata():
    """trace() propagates session_id, user_id, business_scene."""
    tracer = _clean_init(
        app_name="test-app",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )

    with tracer.trace(
        name="task-with-meta",
        session_id="session-123",
        user_id="user-456",
        business_scene="coding_agent",
    ):
        pass

    record = tracer.reporter._queue[-1]
    assert record["session_id"] == "session-123"
    assert record["user_id"] == "user-456"
    assert record["business_scene"] == "coding_agent"
    assert record["app_name"] == "test-app"

    Observability.shutdown()


def test_observability_init_is_idempotent():
    """Repeated init() must not create duplicate tracers or re-patch."""
    tracer1 = _clean_init(
        app_name="app1",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )

    Observability.init(
        app_name="app2",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
    )
    tracer2 = Observability._tracer

    # Same instance — idempotent
    assert tracer1 is tracer2

    Observability.shutdown()
