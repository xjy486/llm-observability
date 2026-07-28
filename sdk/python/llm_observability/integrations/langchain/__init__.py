"""LangChain v1 create_agent auto-instrumentation.

Usage:
    from llm_observability.integrations.langchain import (
        LangChainObservabilityMiddleware,
        observe_agent,
    )
"""

# These imports are enabled once the modules are implemented.
# For now, we use lazy imports to avoid breaking the package.
def __getattr__(name):
    if name == "LangChainObservabilityMiddleware":
        from .middleware import LangChainObservabilityMiddleware
        return LangChainObservabilityMiddleware
    if name == "observe_agent":
        from .agent_wrapper import observe_agent
        return observe_agent
    if name == "observe_runnable":
        from .runnable_wrapper import observe_runnable
        return observe_runnable
    if name == "LangChainObservabilityCallbackHandler":
        from .callback_handler import LangChainObservabilityCallbackHandler
        return LangChainObservabilityCallbackHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "LangChainObservabilityMiddleware",
    "observe_agent",
    "observe_runnable",
    "LangChainObservabilityCallbackHandler",
]
