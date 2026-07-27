"""Phase 2.3: Observability convenience API tests."""
import pytest
from llm_observability import Observability


@pytest.fixture
def init_sdk():
    Observability.init(app_name="test", endpoint="http://localhost:9999", auto_instrument_openai=False)
    yield
    Observability.shutdown()


def test_langchain_middleware_returns_instance(init_sdk):
    mw = Observability.langchain_middleware()
    assert mw is not None
    assert hasattr(mw, "wrap_model_call")
    assert hasattr(mw, "wrap_tool_call")
    assert hasattr(mw, "awrap_model_call")
    assert hasattr(mw, "awrap_tool_call")


def test_instrument_langchain_agent_returns_observed(init_sdk):
    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from typing import ClassVar

    class FakeModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "fake"
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hi"))])
        def bind_tools(self, tools, **kwargs):
            return self

    agent = create_agent(model=FakeModel(), tools=[])
    observed = Observability.instrument_langchain_agent(agent, name="my-agent")
    assert observed is not None
    assert hasattr(observed, "invoke")
    assert hasattr(observed, "stream")


def test_convenience_api_without_init_raises():
    if Observability._initialized:
        Observability.shutdown()
    with pytest.raises(RuntimeError):
        Observability.langchain_middleware()
    with pytest.raises(RuntimeError):
        Observability.instrument_langchain_agent(None)
