"""LangChain Observability Middleware.

Hooks into create_agent's wrap_model_call and wrap_tool_call to create
LLM and TOOL spans.  Does NOT create the Root Trace — that is the
responsibility of ObservedLangChainAgent (spec §15).
"""
import logging
from typing import Any

from .compat import AgentMiddleware, ensure_langchain_available
from .llm_span import LogicalLLMSpan
from .metadata import extract_tool_attributes, extract_tool_name

logger = logging.getLogger("llm_obs.integrations.langchain.middleware")


class LangChainObservabilityMiddleware(AgentMiddleware if AgentMiddleware else object):
    """Middleware that creates LLM/TOOL spans for LangChain create_agent.

    Must be passed to create_agent(middleware=[...]).

    When no active observability context exists, all hooks are noop.
    """

    def wrap_model_call(self, request, handler):
        """Sync model call wrapper — creates LLM span.

        Sets logical_llm_span_active=True for OpenAI Instrumentor dedup.
        """
        from llm_observability import Observability
        if Observability._tracer is None:
            return handler(request)

        from ...context import get_current_context
        current = get_current_context()
        if current is None:
            logger.debug("No active context — model call hook is noop")
            return handler(request)

        try:
            with LogicalLLMSpan(request) as llm_span:
                response = handler(request)
                llm_span.set_response(response)
                return response
        except Exception:
            # If it's the handler's exception, it was already captured by the span.
            # Re-raise to not swallow business exceptions.
            raise

    async def awrap_model_call(self, request, handler):
        """Async model call wrapper — creates LLM span."""
        from llm_observability import Observability
        if Observability._tracer is None:
            return await handler(request)

        from ...context import get_current_context
        current = get_current_context()
        if current is None:
            logger.debug("No active context — async model call hook is noop")
            return await handler(request)

        try:
            with LogicalLLMSpan(request) as llm_span:
                response = await handler(request)
                llm_span.set_response(response)
                return response
        except Exception:
            raise

    def wrap_tool_call(self, request, handler):
        """Sync tool call wrapper — creates TOOL span.

        Reuses Phase 2.2 Tracer.tool() for consistency.
        """
        from llm_observability import Observability
        if Observability._tracer is None:
            return handler(request)

        from ...context import get_current_context
        current = get_current_context()
        if current is None:
            logger.debug("No active context — tool call hook is noop")
            return handler(request)

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

        try:
            with Observability._tracer.tool(
                name=tool_name,
                tool_type="langchain",
                input=tool_args,
                call_id=tool_call_id,
                attributes=framework_attrs,
            ) as tool_span:
                result = handler(request)
                tool_span.set_output(result)
                return result
        except Exception:
            raise

    async def awrap_tool_call(self, request, handler):
        """Async tool call wrapper — creates TOOL span."""
        from llm_observability import Observability
        if Observability._tracer is None:
            return await handler(request)

        from ...context import get_current_context
        current = get_current_context()
        if current is None:
            logger.debug("No active context — async tool call hook is noop")
            return await handler(request)

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

        try:
            with Observability._tracer.tool(
                name=tool_name,
                tool_type="langchain",
                input=tool_args,
                call_id=tool_call_id,
                attributes=framework_attrs,
            ) as tool_span:
                result = await handler(request)
                tool_span.set_output(result)
                return result
        except Exception:
            raise
