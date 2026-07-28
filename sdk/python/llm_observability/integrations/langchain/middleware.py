"""LangChain Observability Middleware.

Hooks into create_agent's wrap_model_call and wrap_tool_call to create
LLM and TOOL spans.  Does NOT create the Root Trace — that is the
responsibility of ObservedLangChainAgent (spec §15).

P0-2: Fail-open — instrumentation failure never changes business result.
      Uses run_with_observation executor that:
      A. Init failure → log, call handler directly
      B. Handler exception → span records error, re-raise
      C. Exit failure → log, don't change result/exception
"""
import logging
from typing import Any, Callable, Optional

from .compat import AgentMiddleware, ensure_langchain_available
from .llm_span import LogicalLLMSpan
from .metadata import extract_tool_attributes, extract_tool_name

logger = logging.getLogger("llm_obs.integrations.langchain.middleware")


def run_with_observation(scope_factory: Callable, handler: Callable):
    """Unified fail-open executor (sync).

    A: Init failure → log, call handler directly.
    B: Handler exception → span records error, re-raise.
    C: Exit failure → log, don't change result/exception.
    """
    scope = None
    handle = None

    try:
        scope = scope_factory()
        handle = scope.__enter__()
    except Exception:
        logger.exception("Instrumentation initialization failed — running handler without observation")
        return handler()

    business_error = None
    try:
        result = handler()
        try:
            if handle and hasattr(handle, "set_response"):
                handle.set_response(result)
            elif handle and hasattr(handle, "set_output"):
                handle.set_output(result)
        except Exception:
            logger.exception("Result capture failed")
        return result
    except BaseException as exc:
        business_error = exc
        raise
    finally:
        try:
            if business_error is not None:
                scope.__exit__(type(business_error), business_error, business_error.__traceback__)
            else:
                scope.__exit__(None, None, None)
        except Exception:
            logger.exception("Instrumentation finalization failed")


async def run_with_observation_async(scope_factory: Callable, handler: Callable):
    """Async fail-open executor."""
    scope = None
    handle = None
    try:
        scope = scope_factory()
        handle = scope.__enter__()
    except Exception:
        logger.exception("Instrumentation initialization failed — running handler without observation")
        return await handler()

    business_error = None
    try:
        result = await handler()
        try:
            if handle and hasattr(handle, "set_response"):
                handle.set_response(result)
            elif handle and hasattr(handle, "set_output"):
                handle.set_output(result)
        except Exception:
            logger.exception("Result capture failed")
        return result
    except BaseException as exc:
        business_error = exc
        raise
    finally:
        try:
            if business_error is not None:
                scope.__exit__(type(business_error), business_error, business_error.__traceback__)
            else:
                scope.__exit__(None, None, None)
        except Exception:
            logger.exception("Instrumentation finalization failed")


class LangChainObservabilityMiddleware(AgentMiddleware if AgentMiddleware else object):
    """Middleware that creates LLM/TOOL spans for LangChain create_agent.

    Must be passed to create_agent(middleware=[...]).

    When no active observability context exists, all hooks are noop.
    P0-2: All hooks use fail-open executor — instrumentation failure
          never changes business execution result.
    """

    def wrap_model_call(self, request, handler):
        """Sync model call wrapper — creates LLM span.

        Sets logical_llm_span_active=True for OpenAI Instrumentor dedup.
        """
        from llm_observability import Observability
        from ...context import get_current_context
        if Observability._tracer is None or get_current_context() is None:
            logger.debug("No active context or tracer — model call hook is noop")
            return handler(request)

        return run_with_observation(
            lambda: LogicalLLMSpan(request),
            lambda: handler(request),
        )

    async def awrap_model_call(self, request, handler):
        """Async model call wrapper — creates LLM span."""
        from llm_observability import Observability
        from ...context import get_current_context
        if Observability._tracer is None or get_current_context() is None:
            logger.debug("No active context or tracer — async model call hook is noop")
            return await handler(request)

        return await run_with_observation_async(
            lambda: LogicalLLMSpan(request),
            lambda: handler(request),
        )

    def wrap_tool_call(self, request, handler):
        """Sync tool call wrapper — creates TOOL span.

        Reuses Phase 2.2 Tracer.tool() for consistency.
        """
        from llm_observability import Observability
        from ...context import get_current_context
        if Observability._tracer is None or get_current_context() is None:
            logger.debug("No active context or tracer — tool call hook is noop")
            return handler(request)

        return run_with_observation(
            lambda: self._tool_scope(request),
            lambda: handler(request),
        )

    async def awrap_tool_call(self, request, handler):
        """Async tool call wrapper — creates TOOL span."""
        from llm_observability import Observability
        from ...context import get_current_context
        if Observability._tracer is None or get_current_context() is None:
            logger.debug("No active context or tracer — async tool call hook is noop")
            return await handler(request)

        return await run_with_observation_async(
            lambda: self._tool_scope(request),
            lambda: handler(request),
        )

    def _tool_scope(self, request):
        """Create a TOOL span context manager for the given request."""
        from llm_observability import Observability
        tool_name = extract_tool_name(request)
        tool_args = None
        tool_call_id = None
        try:
            tc = getattr(request, "tool_call", None)
            if tc and isinstance(tc, dict):
                tool_args = tc.get("args")
                tool_call_id = tc.get("id")
        except Exception:
            pass
        framework_attrs = extract_tool_attributes(request)
        return Observability._tracer.tool(
            name=tool_name,
            tool_type="langchain",
            input=tool_args,
            call_id=tool_call_id,
            attributes=framework_attrs,
        )