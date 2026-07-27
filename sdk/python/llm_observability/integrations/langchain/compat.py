"""LangChain version detection and optional imports.

Centralizes all LangChain-dependent imports so that main business files
remain clean.  When LangChain is not installed, calling
``ensure_langchain_available()`` raises a clear ImportError with install
instructions.
"""
import logging

logger = logging.getLogger("llm_obs.integrations.langchain.compat")

_LANGCHAIN_INSTALL_HINT = (
    'LangChain integration requires optional dependency.\n'
    'Install with:\n\n'
    '    pip install "llm-observability-sdk[langchain]"'
)


def ensure_langchain_available():
    """Raise ImportError with install instructions if LangChain is not installed."""
    try:
        import langchain  # noqa: F401
    except ImportError:
        raise ImportError(_LANGCHAIN_INSTALL_HINT)


# Attempt imports — these will fail gracefully if langchain is not installed.
try:
    from langchain.agents.middleware import (
        AgentMiddleware,
        ModelRequest,
        ModelResponse,
        ToolCallRequest,
    )
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    AgentMiddleware = None
    ModelRequest = None
    ModelResponse = None
    ToolCallRequest = None
    _LANGCHAIN_AVAILABLE = False

try:
    from langgraph.errors import GraphInterrupt
except ImportError:
    GraphInterrupt = None

try:
    from langgraph.types import Command
except ImportError:
    Command = None

try:
    from langchain_core.messages import ToolMessage
except ImportError:
    ToolMessage = None

try:
    from langgraph.runtime import ExecutionInfo
except ImportError:
    ExecutionInfo = None

try:
    import langchain
    LANGCHAIN_VERSION = langchain.__version__ if hasattr(langchain, '__version__') else "unknown"
except ImportError:
    LANGCHAIN_VERSION = "unknown"


def is_langchain_available() -> bool:
    """Check if LangChain is installed and importable."""
    return _LANGCHAIN_AVAILABLE
