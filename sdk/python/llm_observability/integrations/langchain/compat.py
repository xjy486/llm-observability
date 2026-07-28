"""LangChain version detection and optional imports.

Centralizes all LangChain-dependent imports so that main business files
remain clean.  When LangChain is not installed, calling
``ensure_langchain_available()`` raises a clear ImportError with install
instructions.
"""
import asyncio as _asyncio
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


def is_langgraph_interrupt(exc: BaseException) -> bool:
    """P1-2: Check if exception is a LangGraph interrupt (human-in-the-loop), not an error.

    GraphInterrupt and NodeInterrupt are control-flow signals, not errors.
    They should not be recorded as span errors.
    """
    if exc is None:
        return False
    # Check GraphInterrupt (langgraph.errors)
    if GraphInterrupt is not None and isinstance(exc, GraphInterrupt):
        return True
    # Check NodeInterrupt (langgraph.pregel)
    try:
        from langgraph.pregel import NodeInterrupt
        if isinstance(exc, NodeInterrupt):
            return True
    except ImportError:
        pass
    # Fallback: check by class name (for compatibility edge cases)
    exc_class_name = type(exc).__name__
    if exc_class_name in ("GraphInterrupt", "NodeInterrupt"):
        return True
    return False


def is_control_flow_exception(exc: BaseException) -> bool:
    """Check if exception is control flow (not a business error).

    Blocker 2: Unifies control-flow detection across TraceContextManager,
    LogicalLLMSpan, and ToolContextManager.

    Control flow exceptions:
    - GeneratorExit: stream/astream close
    - asyncio.CancelledError: async cancellation
    - GraphInterrupt / NodeInterrupt: LangGraph human-in-the-loop
    """
    if exc is None:
        return False
    if isinstance(exc, GeneratorExit):
        return True
    if isinstance(exc, _asyncio.CancelledError):
        return True
    return is_langgraph_interrupt(exc)