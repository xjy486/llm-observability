"""P1-1: Identity mapping tests."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock, patch
from llm_observability import Observability
from llm_observability.integrations.langchain.agent_wrapper import _resolve_value, _AgentScope


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="identity-test", endpoint="http://localhost:99999")
    yield Observability._tracer
    Observability.shutdown()


def test_session_id_callable_receives_input_and_config():
    captured = []
    def fn(input, config):
        captured.append((input, config))
        return "session-from-callable"
    result = _resolve_value(fn, {"q": "hi"}, {"configurable": {"thread_id": "t1"}})
    assert result == "session-from-callable"
    assert captured == [({"q": "hi"}, {"configurable": {"thread_id": "t1"}})]


def test_user_id_callable_receives_input_and_config():
    def fn(input, config):
        return "user-from-config"
    result = _resolve_value(fn, {}, {"metadata": {"user_id": "u1"}})
    assert result == "user-from-config"


def test_thread_id_maps_to_trace_session_id(init_sdk):
    """When no explicit session_id, thread_id becomes session_id."""
    scope = _AgentScope("test", "auto", None, None, None, config={"configurable": {"thread_id": "thread-xyz"}})
    scope.__enter__()
    try:
        trace_cm = scope._trace_cm
        assert trace_cm is not None
        assert trace_cm._session_id == "thread-xyz"
    finally:
        scope.__exit__(None, None, None)


def test_explicit_session_id_overrides_thread_id(init_sdk):
    scope = _AgentScope("test", "auto", "explicit-session", None, None, config={"configurable": {"thread_id": "thread-xyz"}})
    scope.__enter__()
    try:
        assert scope._trace_cm._session_id == "explicit-session"
    finally:
        scope.__exit__(None, None, None)


def test_callable_failure_is_fail_open():
    def fn(input, config):
        raise RuntimeError("boom")
    result = _resolve_value(fn, {}, {})
    assert result is None


def test_user_id_from_config_metadata(init_sdk):
    """When user_id callable not set, check config.metadata.user_id."""
    scope = _AgentScope("test", "auto", None, None, None, config={"metadata": {"user_id": "user-abc"}})
    scope.__enter__()
    try:
        assert scope._trace_cm._user_id == "user-abc"
    finally:
        scope.__exit__(None, None, None)


def test_business_scene_from_config_metadata(init_sdk):
    scope = _AgentScope("test", "auto", None, None, None, config={"metadata": {"business_scene": "scene1"}})
    scope.__enter__()
    try:
        assert scope._trace_cm._business_scene == "scene1"
    finally:
        scope.__exit__(None, None, None)
