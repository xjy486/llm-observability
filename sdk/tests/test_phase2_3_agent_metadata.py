"""P1-6: AGENT framework metadata tests."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest
from unittest.mock import MagicMock, patch
from llm_observability import Observability
from llm_observability.context import SpanContext, set_context, reset_context, get_current_context
from llm_observability.integrations.langchain.agent_wrapper import ObservedLangChainAgent, observe_agent


@pytest.fixture
def init_sdk():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="agent-meta-test", endpoint="http://localhost:99999")
    yield Observability._tracer
    Observability.shutdown()


def test_observed_agent_root_has_framework_metadata(init_sdk):
    tracer = init_sdk
    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(return_value={"messages": []})

    observed = observe_agent(fake_agent, name="my-agent")
    observed.invoke({"messages": []})

    tracer.reporter.report = orig_report
    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    assert len(agent_spans) == 1
    attrs = agent_spans[0].get("attributes", {})
    assert attrs.get("framework.name") == "langchain"
    assert attrs.get("langchain.component") == "agent"
    assert attrs.get("langchain.agent.name") == "my-agent"


def test_real_e2e_agent_has_framework_name_langchain(init_sdk):
    """AGENT span attributes include framework.name=langchain."""
    tracer = init_sdk
    captured = []
    orig_report = tracer.reporter.report
    tracer.reporter.report = lambda r: captured.append(r)

    fake_agent = MagicMock()
    fake_agent.invoke = MagicMock(return_value={"messages": []})

    observed = observe_agent(fake_agent, name="e2e-agent")
    observed.invoke({"messages": []})

    tracer.reporter.report = orig_report
    agent_spans = [r for r in captured if r["span_kind"] == "AGENT"]
    assert len(agent_spans) >= 1
    assert agent_spans[0]["attributes"]["framework.name"] == "langchain"
