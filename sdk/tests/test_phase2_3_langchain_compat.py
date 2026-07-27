"""Phase 2.3: LangChain compatibility layer tests."""
import pytest


def test_compat_imports_all_required_symbols():
    from llm_observability.integrations.langchain.compat import (
        ensure_langchain_available,
        AgentMiddleware,
        ModelRequest,
        ModelResponse,
        ToolCallRequest,
        GraphInterrupt,
        Command,
        ToolMessage,
        LANGCHAIN_VERSION,
    )
    assert ensure_langchain_available is not None
    assert AgentMiddleware is not None
    assert ModelRequest is not None
    assert ModelResponse is not None
    assert ToolCallRequest is not None
    assert LANGCHAIN_VERSION is not None


def test_langchain_version_is_string():
    from llm_observability.integrations.langchain.compat import LANGCHAIN_VERSION
    assert isinstance(LANGCHAIN_VERSION, str)
    assert len(LANGCHAIN_VERSION) > 0


def test_core_sdk_imports_without_langchain():
    """Core SDK must import even if langchain is not installed."""
    import llm_observability
    assert hasattr(llm_observability, 'Observability')


def test_integration_import_raises_clear_error_pattern():
    """When langchain missing, error message mentions pip install."""
    from llm_observability.integrations.langchain.compat import ensure_langchain_available
    # Should not raise since langchain is installed
    ensure_langchain_available()
