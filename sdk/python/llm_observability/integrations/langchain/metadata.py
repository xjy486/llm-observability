"""LangChain object metadata extraction and normalization.

All functions are fail-open: exceptions return empty/None rather than
propagating to the caller.
"""
import logging
from typing import Any, Optional

logger = logging.getLogger("llm_obs.integrations.langchain.metadata")

# Privacy limits (spec §26)
MAX_TAGS = 50
MAX_TAG_LENGTH = 128
MAX_METADATA_BYTES = 16 * 1024  # 16 KiB
MAX_RUN_NAME_LENGTH = 128
MAX_THREAD_ID_LENGTH = 256


def extract_model_name(model: Any) -> str:
    """Extract model name from a BaseChatModel.

    Order: model.model_name -> model.model -> model._llm_type -> class name.
    """
    for attr in ("model_name", "model", "_llm_type"):
        try:
            val = getattr(model, attr, None)
            if val:
                return str(val)
        except Exception:
            continue
    try:
        return type(model).__name__
    except Exception:
        return "unknown"


def normalize_messages(messages: Any) -> list:
    """Normalize LangChain Message objects to plain JSON dicts.

    Returns [{"type": "human", "content": "...", "tool_calls": [...]}, ...]
    """
    if not messages:
        return []
    result = []
    for msg in messages:
        try:
            entry = {
                "type": getattr(msg, "type", type(msg).__name__.lower()),
                "content": getattr(msg, "content", str(msg)),
            }
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                entry["tool_calls"] = tool_calls
            result.append(entry)
        except Exception as e:
            logger.debug("Failed to normalize message: %s", e)
            result.append({"type": "unknown", "content": "<unserializable>"})
    return result


def extract_token_usage(response: Any) -> dict:
    """Extract token usage from ModelResponse.

    Priority: AIMessage.usage_metadata -> response_metadata.token_usage.
    Returns dict with gen_ai.usage.* keys, or empty dict if not found.
    """
    usage = {}
    try:
        messages = getattr(response, "result", None)
        if messages and len(messages) > 0:
            msg = messages[0]
            # Priority 1: usage_metadata
            um = getattr(msg, "usage_metadata", None)
            if um and isinstance(um, dict):
                if "input_tokens" in um:
                    usage["gen_ai.usage.input_tokens"] = um["input_tokens"]
                if "output_tokens" in um:
                    usage["gen_ai.usage.output_tokens"] = um["output_tokens"]
                if "total_tokens" in um:
                    usage["gen_ai.usage.total_tokens"] = um["total_tokens"]
                return usage

            # Priority 2: response_metadata.token_usage
            rm = getattr(msg, "response_metadata", None)
            if rm and isinstance(rm, dict):
                tu = rm.get("token_usage") or rm.get("usage")
                if tu and isinstance(tu, dict):
                    for src_key, dst_key in [
                        ("prompt_tokens", "gen_ai.usage.input_tokens"),
                        ("completion_tokens", "gen_ai.usage.output_tokens"),
                        ("total_tokens", "gen_ai.usage.total_tokens"),
                    ]:
                        if src_key in tu:
                            usage[dst_key] = tu[src_key]
                    return usage
    except Exception as e:
        logger.debug("Token usage extraction failed: %s", e)
    return usage


def extract_config_metadata(config: Any) -> dict:
    """Extract LangChain RunnableConfig metadata.

    Maps: thread_id, run_name, tags, metadata.
    Applies size limits per spec §26.
    """
    if not config or not isinstance(config, dict):
        return {}

    result = {}
    try:
        configurable = config.get("configurable", {})
        if configurable:
            thread_id = configurable.get("thread_id")
            if thread_id:
                tid = str(thread_id)
                if len(tid) > MAX_THREAD_ID_LENGTH:
                    tid = tid[:MAX_THREAD_ID_LENGTH] + "...[truncated]"
                result["langchain.thread_id"] = tid

        run_name = config.get("run_name")
        if run_name:
            rn = str(run_name)
            if len(rn) > MAX_RUN_NAME_LENGTH:
                rn = rn[:MAX_RUN_NAME_LENGTH] + "...[truncated]"
            result["langchain.run_name"] = rn

        tags = config.get("tags")
        if tags and isinstance(tags, list):
            trimmed = tags[:MAX_TAGS]
            result["langchain.tags"] = [
                str(t)[:MAX_TAG_LENGTH] if len(str(t)) > MAX_TAG_LENGTH else str(t)
                for t in trimmed
            ]

        metadata = config.get("metadata")
        if metadata and isinstance(metadata, dict):
            result["langchain.metadata"] = metadata
    except Exception as e:
        logger.debug("Config metadata extraction failed: %s", e)
    return result


def extract_model_attributes(request: Any) -> dict:
    """Extract framework attributes from a ModelRequest.

    Returns dict with framework.name, gen_ai.request.model, etc.
    """
    attrs = {}
    try:
        attrs["framework.name"] = "langchain"
        from .compat import LANGCHAIN_VERSION
        attrs["framework.version"] = LANGCHAIN_VERSION
        attrs["langchain.component"] = "model"

        model = getattr(request, "model", None)
        if model:
            attrs["gen_ai.request.model"] = extract_model_name(model)
            attrs["langchain.model.class"] = type(model).__name__
            attrs["gen_ai.operation.name"] = "chat"

            # Provider name from model class
            model_class = type(model).__name__.lower()
            if "openai" in model_class:
                attrs["gen_ai.provider.name"] = "openai"
            elif "anthropic" in model_class:
                attrs["gen_ai.provider.name"] = "anthropic"
            elif "google" in model_class or "gemini" in model_class:
                attrs["gen_ai.provider.name"] = "google"
            elif "bedrock" in model_class:
                attrs["gen_ai.provider.name"] = "aws_bedrock"

        # Attempt number from runtime.execution_info
        runtime = getattr(request, "runtime", None)
        if runtime:
            ei = getattr(runtime, "execution_info", None)
            if ei:
                attempt = getattr(ei, "node_attempt", None)
                if attempt is not None:
                    attrs["langchain.attempt"] = attempt
    except Exception as e:
        logger.debug("Model attributes extraction failed: %s", e)
    return attrs


def extract_tool_attributes(request: Any) -> dict:
    """Extract framework attributes from a ToolCallRequest.

    Returns dict with framework.name, tool call_id, etc.
    """
    attrs = {}
    try:
        attrs["framework.name"] = "langchain"
        from .compat import LANGCHAIN_VERSION
        attrs["framework.version"] = LANGCHAIN_VERSION
        attrs["langchain.component"] = "tool"

        tool_call = getattr(request, "tool_call", None)
        if tool_call and isinstance(tool_call, dict):
            tc_id = tool_call.get("id")
            if tc_id:
                attrs["langchain.tool.call_id"] = tc_id
    except Exception as e:
        logger.debug("Tool attributes extraction failed: %s", e)
    return attrs


def extract_tool_name(request: Any) -> str:
    """Extract tool name from ToolCallRequest.

    Order: request.tool_call["name"] -> request.tool.name -> request.tool.__class__.__name__.
    Falls back to 'langchain_tool' with a name_missing flag.
    """
    try:
        tc = getattr(request, "tool_call", None)
        if tc and isinstance(tc, dict):
            name = tc.get("name")
            if name:
                return name

        tool = getattr(request, "tool", None)
        if tool:
            name = getattr(tool, "name", None)
            if name:
                return name
            return type(tool).__name__
    except Exception:
        pass
    return "langchain_tool"
