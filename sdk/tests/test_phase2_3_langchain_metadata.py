"""Phase 2.3: LangChain metadata extraction tests."""
import pytest
from unittest.mock import MagicMock
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage


def test_extract_model_name_from_model_name_attr():
    from llm_observability.integrations.langchain.metadata import extract_model_name
    model = MagicMock()
    model.model_name = "gpt-4o"
    assert extract_model_name(model) == "gpt-4o"


def test_extract_model_name_from_model_attr():
    from llm_observability.integrations.langchain.metadata import extract_model_name
    model = MagicMock()
    del model.model_name
    model.model = "gpt-4o-mini"
    assert extract_model_name(model) == "gpt-4o-mini"


def test_extract_model_name_from_llm_type():
    from llm_observability.integrations.langchain.metadata import extract_model_name
    model = MagicMock()
    del model.model_name
    del model.model
    model._llm_type = "openai-chat"
    assert extract_model_name(model) == "openai-chat"


def test_extract_model_name_from_class_name():
    from llm_observability.integrations.langchain.metadata import extract_model_name
    model = MagicMock()
    del model.model_name
    del model.model
    del model._llm_type
    model.__class__.__name__ = "ChatOpenAI"
    assert extract_model_name(model) == "ChatOpenAI"


def test_normalize_messages_basic():
    from llm_observability.integrations.langchain.metadata import normalize_messages
    messages = [
        HumanMessage(content="Hello"),
        AIMessage(content="Hi there"),
    ]
    result = normalize_messages(messages)
    assert len(result) == 2
    assert result[0]["type"] == "human"
    assert result[0]["content"] == "Hello"
    assert result[1]["type"] == "ai"
    assert result[1]["content"] == "Hi there"


def test_normalize_messages_with_tool_calls():
    from llm_observability.integrations.langchain.metadata import normalize_messages
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "search", "args": {"q": "test"}, "id": "c1", "type": "tool_call"}],
    )
    result = normalize_messages([msg])
    assert result[0]["tool_calls"] == [{"name": "search", "args": {"q": "test"}, "id": "c1", "type": "tool_call"}]


def test_extract_token_usage_from_usage_metadata():
    from llm_observability.integrations.langchain.metadata import extract_token_usage
    response = MagicMock()
    msg = MagicMock()
    msg.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    response.result = [msg]
    usage = extract_token_usage(response)
    assert usage["gen_ai.usage.input_tokens"] == 10
    assert usage["gen_ai.usage.output_tokens"] == 5
    assert usage["gen_ai.usage.total_tokens"] == 15


def test_extract_token_usage_missing():
    from llm_observability.integrations.langchain.metadata import extract_token_usage
    response = MagicMock()
    msg = MagicMock()
    msg.usage_metadata = None
    msg.response_metadata = {}
    response.result = [msg]
    usage = extract_token_usage(response)
    assert usage == {}


def test_extract_config_metadata():
    from llm_observability.integrations.langchain.metadata import extract_config_metadata
    config = {
        "configurable": {"thread_id": "t123"},
        "run_name": "my-agent",
        "tags": ["prod", "v1"],
        "metadata": {"user": "alice"},
    }
    result = extract_config_metadata(config)
    assert result["langchain.thread_id"] == "t123"
    assert result["langchain.run_name"] == "my-agent"
    assert result["langchain.tags"] == ["prod", "v1"]
    assert result["langchain.metadata"] == {"user": "alice"}


def test_extract_config_metadata_empty():
    from llm_observability.integrations.langchain.metadata import extract_config_metadata
    result = extract_config_metadata(None)
    assert result == {}


def test_extract_tool_name_from_tool_call():
    from llm_observability.integrations.langchain.metadata import extract_tool_name
    request = MagicMock()
    request.tool_call = {"name": "search", "args": {"q": "test"}, "id": "c1", "type": "tool_call"}
    request.tool = None
    assert extract_tool_name(request) == "search"


def test_extract_tool_name_from_tool_object():
    from llm_observability.integrations.langchain.metadata import extract_tool_name
    request = MagicMock()
    request.tool_call = {"args": {}, "id": "c1"}
    request.tool = MagicMock()
    request.tool.name = "calculator"
    assert extract_tool_name(request) == "calculator"


def test_extract_tool_name_fallback():
    from llm_observability.integrations.langchain.metadata import extract_tool_name
    request = MagicMock()
    request.tool_call = {}
    request.tool = None
    assert extract_tool_name(request) == "langchain_tool"


def test_extract_model_attributes():
    from llm_observability.integrations.langchain.metadata import extract_model_attributes
    request = MagicMock()
    request.model = MagicMock()
    del request.model.model_name
    del request.model.model
    request.model._llm_type = "openai-chat"
    request.model.__class__.__name__ = "ChatOpenAI"
    request.runtime = MagicMock()
    request.runtime.execution_info = MagicMock()
    request.runtime.execution_info.node_attempt = 2

    attrs = extract_model_attributes(request)
    assert attrs["framework.name"] == "langchain"
    assert attrs["gen_ai.request.model"] == "openai-chat"
    assert attrs["langchain.model.class"] == "ChatOpenAI"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["langchain.attempt"] == 2


def test_extract_tool_attributes():
    from llm_observability.integrations.langchain.metadata import extract_tool_attributes
    request = MagicMock()
    request.tool_call = {"name": "search", "args": {}, "id": "call_abc", "type": "tool_call"}
    request.tool = None

    attrs = extract_tool_attributes(request)
    assert attrs["framework.name"] == "langchain"
    assert attrs["langchain.component"] == "tool"
    assert attrs["langchain.tool.call_id"] == "call_abc"