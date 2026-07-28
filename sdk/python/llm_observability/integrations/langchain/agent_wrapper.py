"""Observed LangChain Agent Wrapper.

Wraps a create_agent output to create/reuse an AGENT Root Trace for
each invoke/ainvoke/stream/astream lifecycle.

Spec §10-14: One invoke = one Trace. Stream uses generator pattern.
Spec §11: root_mode auto/create/require_existing.
"""
import logging
from typing import Any, Optional, Callable, Union

from ...context import get_current_context, set_context, reset_context, SpanContext
from ...spans import SpanKind
from .metadata import sanitize_langchain_config_metadata

logger = logging.getLogger("llm_obs.integrations.langchain.agent_wrapper")

_VALID_ROOT_MODES = {"auto", "create", "require_existing"}


def observe_agent(
    agent: Any,
    name: str = "langchain.agent",
    root_mode: str = "auto",
    session_id: Optional[Union[str, Callable]] = None,
    user_id: Optional[Union[str, Callable]] = None,
    business_scene: Optional[Union[str, Callable]] = None,
) -> "ObservedLangChainAgent":
    """Wrap a LangChain agent with observability.

    Args:
        agent: A create_agent() result (CompiledStateGraph).
        name: Agent name for the AGENT span (default 'langchain.agent').
        root_mode: 'auto' (create if no context, reuse if exists),
                   'create' (must be no context, else error),
                   'require_existing' (must have context).
        session_id: Optional string or callable.
        user_id: Optional string or callable.
        business_scene: Optional string or callable.

    Returns:
        ObservedLangChainAgent wrapping the input agent.
    """
    if root_mode not in _VALID_ROOT_MODES:
        raise ValueError(
            f"Invalid root_mode '{root_mode}'. Must be one of: {_VALID_ROOT_MODES}"
        )

    if not hasattr(agent, "invoke") or not hasattr(agent, "ainvoke"):
        raise ValueError(
            "Agent must support invoke/ainvoke. "
            "Pass the result of langchain.agents.create_agent()."
        )

    return ObservedLangChainAgent(
        agent=agent,
        name=name,
        root_mode=root_mode,
        session_id=session_id,
        user_id=user_id,
        business_scene=business_scene,
    )


def _resolve_value(value: Any, input: Any = None, config: Any = None) -> Any:
    """Resolve a string-or-callable value.

    P1-1: Callable can accept (input, config), (config), or ().
    Falls back gracefully through argument signatures.
    Fail-open: returns None on callable exception.
    """
    if not callable(value):
        return value
    for args in ((input, config), (config,), ()):
        try:
            return value(*args)
        except TypeError:
            continue
        except Exception:
            return None
    return None


def _sanitize_identity_value(value: Any, max_length: int = 256) -> Optional[str]:
    """Blocker 1: Unified identity field sanitization.

    Applies _mask_string_patterns before truncation so sensitive text
    patterns (Bearer xxx, sk-xxx, token=xxx) are redacted in ALL identity
    fields, not just span attributes.

    Used for: session_id, user_id, business_scene, thread_id auto-mapping,
    callable return values, and config.metadata.* extraction.
    """
    if value is None:
        return None
    try:
        from ...utils.masking import _mask_string_patterns
        text = str(value)
        text = _mask_string_patterns(text)
        return text[:max_length]
    except Exception:
        try:
            return str(value)[:max_length]
        except Exception:
            return None


def _resolve_session_id(session_id, input, config):
    """P1-1: session_id resolution order:
    explicit > callable(input,config) > thread_id from config.configurable > None.

    Blocker 1: All resolved values pass through _sanitize_identity_value.
    """
    if session_id is not None:
        resolved = _resolve_value(session_id, input, config)
        return _sanitize_identity_value(resolved)
    if config and isinstance(config, dict):
        configurable = config.get("configurable", {})
        if configurable:
            tid = configurable.get("thread_id")
            if tid:
                return _sanitize_identity_value(tid, max_length=256)
    return None


def _resolve_user_id(user_id, input, config):
    """P1-1: user_id resolution order:
    explicit > callable(input,config) > config.metadata.user_id > config.metadata.user > None.

    Blocker 1: All resolved values pass through _sanitize_identity_value.
    """
    if user_id is not None:
        resolved = _resolve_value(user_id, input, config)
        return _sanitize_identity_value(resolved)
    if config and isinstance(config, dict):
        metadata = config.get("metadata", {})
        if metadata and isinstance(metadata, dict):
            for key in ("user_id", "user"):
                val = metadata.get(key)
                if val:
                    return _sanitize_identity_value(val)
    return None


def _resolve_business_scene(business_scene, input, config):
    """P1-1: business_scene resolution order:
    explicit > callable(input,config) > config.metadata.business_scene > None.

    Blocker 1: All resolved values pass through _sanitize_identity_value.
    """
    if business_scene is not None:
        resolved = _resolve_value(business_scene, input, config)
        return _sanitize_identity_value(resolved)
    if config and isinstance(config, dict):
        metadata = config.get("metadata", {})
        if metadata and isinstance(metadata, dict):
            val = metadata.get("business_scene")
            if val:
                return _sanitize_identity_value(val)
    return None


class _AgentScope:
    """Context manager that creates/reuses an AGENT Root Trace.

    In 'auto' mode: creates a trace if no active context, reuses if exists.
    In 'create' mode: creates a trace, errors if context already active.
    In 'require_existing' mode: uses existing context, errors if none.
    """

    def __init__(self, name, root_mode, session_id, user_id, business_scene, config=None, input=None):
        self._name = name
        self._root_mode = root_mode
        self._session_id = session_id
        self._user_id = user_id
        self._business_scene = business_scene
        self._config = config
        self._input = input
        self._trace_cm = None
        self._created_trace = False
        self._token = None
        self._existing_ctx = None

    def __enter__(self):
        from llm_observability import Observability
        if Observability._tracer is None:
            logger.debug("Observability not initialized — agent scope is noop")
            return self

        current = get_current_context()

        if self._root_mode == "require_existing":
            if current is None:
                raise RuntimeError(
                    "root_mode='require_existing' but no active trace found. "
                    "Create a trace with Observability.trace() first."
                )
            self._existing_ctx = current
            return self

        if self._root_mode == "create":
            if current is not None:
                raise RuntimeError(
                    "root_mode='create' but an active trace already exists. "
                    "Use root_mode='auto' to reuse existing traces."
                )

        # auto mode or create mode without existing context
        if current is not None:
            # Reuse existing context
            self._existing_ctx = current
            return self

        # Create new trace
        self._trace_cm = Observability.trace(
            name=self._name,
            session_id=_resolve_session_id(self._session_id, self._input, self._config),
            user_id=_resolve_user_id(self._user_id, self._input, self._config),
            business_scene=_resolve_business_scene(self._business_scene, self._input, self._config),
        )
        self._trace_cm.__enter__()
        self._created_trace = True

        # Override span_name to agent.<name> (spec §10)
        # TraceContextManager defaults to "agent.run" for root spans;
        # we set the correct name here.
        if self._trace_cm._span is not None:
            self._trace_cm._span.span_name = f"agent.{self._name}"

            # P1-6: Add AGENT framework metadata
            from .compat import LANGCHAIN_VERSION
            self._trace_cm._span.set_attribute("framework.name", "langchain")
            self._trace_cm._span.set_attribute("framework.version", LANGCHAIN_VERSION)
            self._trace_cm._span.set_attribute("langchain.component", "agent")
            self._trace_cm._span.set_attribute("langchain.agent.name", self._name)

        # Add config metadata to the span (P0-3: sanitized)
        if self._config:
            try:
                from llm_observability import Observability
                strategy = "masked"
                if Observability._tracer and Observability._tracer.config:
                    strategy = Observability._tracer.config.payload_strategy
                attrs = sanitize_langchain_config_metadata(self._config, strategy)
                span = self._trace_cm._span
                for k, v in attrs.items():
                    span.set_attribute(k, v)
            except Exception as e:
                logger.debug("Config metadata sanitization failed: %s", e)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._trace_cm is not None and self._created_trace:
            self._trace_cm.__exit__(exc_type, exc_val, exc_tb)
        return False


class ObservedLangChainAgent:
    """Wraps a LangChain agent with automatic AGENT Root Trace creation.

    Delegates invoke/ainvoke/stream/astream with trace lifecycle management.
    All other attributes are transparently delegated via __getattr__.
    """

    def __init__(
        self,
        agent: Any,
        name: str = "langchain.agent",
        root_mode: str = "auto",
        session_id: Optional[Union[str, Callable]] = None,
        user_id: Optional[Union[str, Callable]] = None,
        business_scene: Optional[Union[str, Callable]] = None,
    ):
        self._agent = agent
        self._name = name
        self._root_mode = root_mode
        self._session_id = session_id
        self._user_id = user_id
        self._business_scene = business_scene

    def invoke(self, input, config=None, **kwargs):
        """Synchronous invoke with AGENT trace lifecycle."""
        with _AgentScope(
            self._name, self._root_mode,
            self._session_id, self._user_id, self._business_scene,
            config=config, input=input,
        ):
            return self._agent.invoke(input, config=config, **kwargs)

    async def ainvoke(self, input, config=None, **kwargs):
        """Async invoke with AGENT trace lifecycle."""
        with _AgentScope(
            self._name, self._root_mode,
            self._session_id, self._user_id, self._business_scene,
            config=config, input=input,
        ):
            return await self._agent.ainvoke(input, config=config, **kwargs)

    def stream(self, input, config=None, **kwargs):
        """Synchronous stream with AGENT trace lifecycle.

        Uses generator pattern so the trace covers the full iteration.
        The trace ends when the generator is exhausted, closed, or errors.
        """
        def _generator():
            with _AgentScope(
                self._name, self._root_mode,
                self._session_id, self._user_id, self._business_scene,
                config=config, input=input,
            ):
                try:
                    yield from self._agent.stream(input, config=config, **kwargs)
                except GeneratorExit:
                    raise
                except Exception:
                    raise

        return _generator()

    async def astream(self, input, config=None, **kwargs):
        """Async stream with AGENT trace lifecycle.

        Uses async generator pattern so the trace covers the full iteration.
        """
        with _AgentScope(
            self._name, self._root_mode,
            self._session_id, self._user_id, self._business_scene,
            config=config, input=input,
        ):
            try:
                async for item in self._agent.astream(input, config=config, **kwargs):
                    yield item
            except GeneratorExit:
                raise
            except Exception:
                raise

    def __getattr__(self, name: str):
        """Transparently delegate unknown attributes to the underlying agent."""
        return getattr(self._agent, name)