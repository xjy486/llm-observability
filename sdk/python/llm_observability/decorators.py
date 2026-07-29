"""Unified Decorator Runtime — Phase 2.5.

Public decorators:
    @agent()   -> AGENT root span (operation.type=agent)
    @chain()   -> TASK span (task.type=chain) — requires active trace
    @task()    -> TASK span (task.type=task)
    @tool()    -> TOOL span (reuses Phase 2.2 ToolRuntime)
    @llm()     -> LLM span (logical_llm_span_active=True, dedup with OpenAI)

All decorators share a single runtime:
    Input Binder -> Output Capture -> Streaming Wrapper ->
    Privacy Pipeline -> Association Resolver -> Context Cleanup

Supported callables: sync, async, sync generator, async generator,
instance/class/static methods.

Telemetry errors never change business return value or exception. Only
configuration errors (SDK not init + fail_open=False) are raised.

SDK not initialized:
    fail_open=True  -> warning + business proceeds
    fail_open=False -> RuntimeError
"""
import functools
import inspect as _inspect
import logging
from typing import Any, Callable, Optional, Union

from .context import (
    SpanContext, get_current_context, set_context, reset_context,
)
from .spans import Span, SpanKind
from .tool import _safe_tool_error_message as _safe_error_message, _bind_arguments
from .association import get_association_properties
from .utils.ids import generate_trace_id, generate_span_id

logger = logging.getLogger("llm_obs.decorators")


# ── Helpers ──

def _is_sync_generator(func) -> bool:
    return _inspect.isgeneratorfunction(func)


def _is_async_generator(func) -> bool:
    return _inspect.isasyncgenfunction(func)


def _is_async(func) -> bool:
    return _inspect.iscoroutinefunction(func)


def _control_flow_exception(exc_type, exc_val) -> bool:
    """Detect control-flow (non-error) exceptions."""
    if exc_type is None:
        return False
    try:
        from .integrations.langchain.compat import is_control_flow_exception
        return is_control_flow_exception(exc_val)
    except ImportError:
        import asyncio as _asyncio
        return (
            exc_type is GeneratorExit
            or (hasattr(_asyncio, 'CancelledError') and exc_type is _asyncio.CancelledError)
        )


def _apply_association(span: Span, explicit: Optional[dict] = None):
    """Apply association properties to a span.

    Priority: span explicit > decorator explicit > association context.
    """
    assoc = get_association_properties()
    if explicit:
        user = explicit.get("user") or explicit.get("user_id")
        session_id = explicit.get("session_id")
        message_id = explicit.get("message_id")
        business = explicit.get("business_scenario") or explicit.get("business_scene")
    else:
        user = None
        session_id = None
        message_id = None
        business = None

    # Association context fills gaps
    if user is None:
        user = assoc.user
    if session_id is None:
        session_id = assoc.session_id
    if message_id is None:
        message_id = assoc.message_id
    if business is None:
        business = assoc.business_scenario

    if user is not None and span.user_id is None:
        span.user_id = user
    if session_id is not None and span.session_id is None:
        span.session_id = session_id
    if message_id is not None and span.message_id is None:
        span.message_id = message_id
    if business is not None and span.business_scene is None:
        span.business_scene = business


def _get_tracer():
    """Get the current tracer, or None if SDK not initialized."""
    from llm_observability import Observability
    return Observability._tracer


def _check_initialized(fail_open: bool) -> bool:
    """Return True if SDK is initialized; otherwise handle per fail_open."""
    from llm_observability import Observability
    if Observability._initialized and Observability._tracer is not None:
        return True
    if fail_open:
        logger.warning("Observability not initialized — decorator running without observation")
        return False
    raise RuntimeError("Observability.init() must be called before using decorators (fail_open=False)")


# ── AGENT decorator ──

def agent(
    name: Optional[str] = None,
    nested_mode: str = "error",
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    message_id: Optional[str] = None,
    business_scenario: Optional[str] = None,
    fail_open: bool = True,
):
    """Decorator that creates an AGENT root span.

    nested_mode:
        'error' (default) — raises if an active trace already exists
        'reuse' — reuses the current trace, adds sdk.agent.reused event
    """
    def decorator(func):
        explicit = {
            "user_id": user_id, "session_id": session_id,
            "message_id": message_id, "business_scenario": business_scenario,
        }

        if _is_async(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await _run_agent_async(func, args, kwargs, name, nested_mode, explicit, fail_open)
            return async_wrapper

        if _is_async_generator(func):
            @functools.wraps(func)
            async def async_gen_wrapper(*args, **kwargs):
                async for item in _run_agent_async_gen(func, args, kwargs, name, nested_mode, explicit, fail_open):
                    yield item
            return async_gen_wrapper

        if _is_sync_generator(func):
            @functools.wraps(func)
            def sync_gen_wrapper(*args, **kwargs):
                yield from _run_agent_sync_gen(func, args, kwargs, name, nested_mode, explicit, fail_open)
            return sync_gen_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            return _run_agent_sync(func, args, kwargs, name, nested_mode, explicit, fail_open)
        return sync_wrapper

    return decorator


def _create_agent_span(func_name, nested_mode, explicit, fail_open):
    """Create (or reuse) the AGENT root span. Returns (span, token, tracer, reused)."""
    if not _check_initialized(fail_open):
        return None, None, None, False
    tracer = _get_tracer()
    current = get_current_context()

    if current is not None:
        # An active trace already exists
        if nested_mode == "error":
            raise RuntimeError(
                "@agent: an active trace already exists. Use nested_mode='reuse' to reuse it."
            )
        # reuse mode: don't create a new span, just return None to signal reuse
        return None, None, tracer, True

    trace_id = generate_trace_id()
    span_id = generate_span_id()
    ctx = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        span_kind=SpanKind.AGENT,
        sampled=True,
    )
    token = set_context(ctx)

    import random
    sampled = random.random() < tracer.config.sample_rate

    span = Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        span_name=f"agent.{func_name}",
        span_kind=SpanKind.AGENT,
        app_name=tracer.config.app_name,
    )
    span.set_attribute("operation.type", "agent")
    _apply_association(span, explicit)
    span.start()

    try:
        from .span_registry import register_span_event_sink
        register_span_event_sink(span)
    except Exception:
        pass

    # Update context sampled flag
    # Note: SpanContext is frozen-ish; we set sampled on the span record instead
    return span, token, tracer, False


def _finalize_agent_span(span, token, tracer, reused, exc_type, exc_val, exc_tb):
    """Finalize the AGENT span and restore context."""
    if reused:
        return
    if span is None:
        if token is not None:
            reset_context(token)
        return
    try:
        try:
            if exc_type is not None and not _control_flow_exception(exc_type, exc_val):
                span.set_error(
                    error_type=exc_type.__name__,
                    error_message=_safe_error_message(exc_val) if exc_val else "",
                )
            else:
                span.set_status("OK")
            span.end()
            if get_current_context() is not None and get_current_context().sampled:
                tracer.reporter.report(span.to_record())
        except Exception:
            logger.exception("AGENT decorator finalization failed")
    finally:
        try:
            from .span_registry import unregister_span_event_sink
            unregister_span_event_sink(span.trace_id, span.span_id)
        except Exception:
            pass
        if token is not None:
            reset_context(token)


def _run_agent_sync(func, args, kwargs, name, nested_mode, explicit, fail_open):
    func_name = name or func.__name__
    span, token, tracer, reused = _create_agent_span(func_name, nested_mode, explicit, fail_open)
    if span is None and token is None and not reused:
        # SDK not init + fail_open — run business only
        return func(*args, **kwargs)
    if reused:
        # Add reuse event
        current = get_current_context()
        try:
            from .span_registry import get_span_event_sink
            sink = get_span_event_sink(current.trace_id, current.span_id) if current else None
            if sink:
                sink.add_event("sdk.agent.reused")
        except Exception:
            pass
    try:
        result = func(*args, **kwargs)
        return result
    except BaseException:
        raise
    finally:
        _finalize_agent_span(span, token, tracer, reused, *sys_exc_info())


async def _run_agent_async(func, args, kwargs, name, nested_mode, explicit, fail_open):
    import sys as _sys
    func_name = name or func.__name__
    span, token, tracer, reused = _create_agent_span(func_name, nested_mode, explicit, fail_open)
    if span is None and token is None and not reused:
        return await func(*args, **kwargs)
    if reused:
        current = get_current_context()
        try:
            from .span_registry import get_span_event_sink
            sink = get_span_event_sink(current.trace_id, current.span_id) if current else None
            if sink:
                sink.add_event("sdk.agent.reused")
        except Exception:
            pass
    try:
        result = await func(*args, **kwargs)
        return result
    except BaseException:
        raise
    finally:
        _finalize_agent_span(span, token, tracer, reused, *_sys.exc_info())


def _run_agent_sync_gen(func, args, kwargs, name, nested_mode, explicit, fail_open):
    import sys as _sys
    func_name = name or func.__name__
    span, token, tracer, reused = _create_agent_span(func_name, nested_mode, explicit, fail_open)
    if span is None and token is None and not reused:
        yield from func(*args, **kwargs)
        return
    try:
        yield from func(*args, **kwargs)
    except BaseException:
        raise
    finally:
        _finalize_agent_span(span, token, tracer, reused, *_sys.exc_info())


async def _run_agent_async_gen(func, args, kwargs, name, nested_mode, explicit, fail_open):
    import sys as _sys
    func_name = name or func.__name__
    span, token, tracer, reused = _create_agent_span(func_name, nested_mode, explicit, fail_open)
    if span is None and token is None and not reused:
        async for item in func(*args, **kwargs):
            yield item
        return
    try:
        async for item in func(*args, **kwargs):
            yield item
    except BaseException:
        raise
    finally:
        _finalize_agent_span(span, token, tracer, reused, *_sys.exc_info())


def sys_exc_info():
    import sys
    return sys.exc_info()


# ── TASK decorator (@chain / @task) ──

def _task_decorator(task_type: str, name: Optional[str] = None, fail_open: bool = True, **extra):
    """Shared factory for @chain and @task."""
    def decorator(func):
        if _is_async(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await _run_task_async(func, args, kwargs, task_type, name, fail_open, extra)
            return async_wrapper

        if _is_async_generator(func):
            @functools.wraps(func)
            async def async_gen_wrapper(*args, **kwargs):
                async for item in _run_task_async_gen(func, args, kwargs, task_type, name, fail_open, extra):
                    yield item
            return async_gen_wrapper

        if _is_sync_generator(func):
            @functools.wraps(func)
            def sync_gen_wrapper(*args, **kwargs):
                yield from _run_task_sync_gen(func, args, kwargs, task_type, name, fail_open, extra)
            return sync_gen_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            return _run_task_sync(func, args, kwargs, task_type, name, fail_open, extra)
        return sync_wrapper

    return decorator


def chain(name: Optional[str] = None, fail_open: bool = True, **kwargs):
    """Decorator that creates a TASK span with task.type=chain.

    Requires an active trace. Without one, behaves per fail_open (no auto AGENT).
    """
    return _task_decorator("chain", name=name, fail_open=fail_open, **kwargs)


def task(name: Optional[str] = None, fail_open: bool = True, **kwargs):
    """Decorator that creates a TASK span with task.type=task."""
    return _task_decorator("task", name=name, fail_open=fail_open, **kwargs)


def _run_task_sync(func, args, kwargs, task_type, name, fail_open, extra):
    if not _check_initialized(fail_open):
        return func(*args, **kwargs)
    tracer = _get_tracer()
    current = get_current_context()
    if current is None:
        if fail_open:
            logger.warning("@%s: no active trace — running without observation", task_type)
            return func(*args, **kwargs)
        raise RuntimeError(f"@{task_type}: requires an active trace")
    func_name = name or func.__name__
    from .task import TaskContextManager
    bound_input = _bind_arguments(func, args, kwargs)
    with TaskContextManager(
        tracer=tracer, name=func_name, task_type=task_type, input=bound_input,
    ) as handle:
        result = func(*args, **kwargs)
        handle.set_output(result)
        return result


async def _run_task_async(func, args, kwargs, task_type, name, fail_open, extra):
    if not _check_initialized(fail_open):
        return await func(*args, **kwargs)
    tracer = _get_tracer()
    current = get_current_context()
    if current is None:
        if fail_open:
            logger.warning("@%s: no active trace — running without observation", task_type)
            return await func(*args, **kwargs)
        raise RuntimeError(f"@{task_type}: requires an active trace")
    func_name = name or func.__name__
    from .task import TaskContextManager
    bound_input = _bind_arguments(func, args, kwargs)
    with TaskContextManager(
        tracer=tracer, name=func_name, task_type=task_type, input=bound_input,
    ) as handle:
        result = await func(*args, **kwargs)
        handle.set_output(result)
        return result


def _run_task_sync_gen(func, args, kwargs, task_type, name, fail_open, extra):
    if not _check_initialized(fail_open):
        yield from func(*args, **kwargs)
        return
    tracer = _get_tracer()
    current = get_current_context()
    if current is None:
        if fail_open:
            logger.warning("@%s: no active trace — running without observation", task_type)
            yield from func(*args, **kwargs)
            return
        raise RuntimeError(f"@{task_type}: requires an active trace")
    func_name = name or func.__name__
    from .task import TaskContextManager
    bound_input = _bind_arguments(func, args, kwargs)
    with TaskContextManager(
        tracer=tracer, name=func_name, task_type=task_type, input=bound_input,
    ) as handle:
        result = list(func(*args, **kwargs))
        handle.set_output(result)
        yield from result


async def _run_task_async_gen(func, args, kwargs, task_type, name, fail_open, extra):
    if not _check_initialized(fail_open):
        async for item in func(*args, **kwargs):
            yield item
        return
    tracer = _get_tracer()
    current = get_current_context()
    if current is None:
        if fail_open:
            logger.warning("@%s: no active trace — running without observation", task_type)
            async for item in func(*args, **kwargs):
                yield item
            return
        raise RuntimeError(f"@{task_type}: requires an active trace")
    func_name = name or func.__name__
    from .task import TaskContextManager
    bound_input = _bind_arguments(func, args, kwargs)
    collected = []
    with TaskContextManager(
        tracer=tracer, name=func_name, task_type=task_type, input=bound_input,
    ) as handle:
        async for item in func(*args, **kwargs):
            collected.append(item)
            yield item
        handle.set_output(collected)


# ── TOOL decorator (reuses Phase 2.2) ──

def tool(name: Optional[str] = None, tool_type: Optional[str] = None, fail_open: bool = True, **kwargs):
    """Decorator that creates a TOOL span. Reuses Phase 2.2 ToolRuntime."""
    def decorator(func):
        if _is_async(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await _run_tool_async(func, args, kwargs, name, tool_type, fail_open)
            return async_wrapper

        if _is_async_generator(func):
            @functools.wraps(func)
            async def async_gen_wrapper(*args, **kwargs):
                async for item in _run_tool_async_gen(func, args, kwargs, name, tool_type, fail_open):
                    yield item
            return async_gen_wrapper

        if _is_sync_generator(func):
            @functools.wraps(func)
            def sync_gen_wrapper(*args, **kwargs):
                yield from _run_tool_sync_gen(func, args, kwargs, name, tool_type, fail_open)
            return sync_gen_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            return _run_tool_sync(func, args, kwargs, name, tool_type, fail_open)
        return sync_wrapper

    return decorator


def _run_tool_sync(func, args, kwargs, name, tool_type, fail_open):
    if not _check_initialized(fail_open):
        return func(*args, **kwargs)
    tracer = _get_tracer()
    if get_current_context() is None:
        if fail_open:
            logger.warning("@tool: no active trace — running without observation")
            return func(*args, **kwargs)
        raise RuntimeError("@tool: requires an active trace")
    func_name = name or func.__name__
    bound_input = _bind_arguments(func, args, kwargs)
    with tracer.tool(name=func_name, tool_type=tool_type, input=bound_input) as handle:
        result = func(*args, **kwargs)
        handle.set_output(result)
        return result


async def _run_tool_async(func, args, kwargs, name, tool_type, fail_open):
    if not _check_initialized(fail_open):
        return await func(*args, **kwargs)
    tracer = _get_tracer()
    if get_current_context() is None:
        if fail_open:
            logger.warning("@tool: no active trace — running without observation")
            return await func(*args, **kwargs)
        raise RuntimeError("@tool: requires an active trace")
    func_name = name or func.__name__
    bound_input = _bind_arguments(func, args, kwargs)
    with tracer.tool(name=func_name, tool_type=tool_type, input=bound_input) as handle:
        result = await func(*args, **kwargs)
        handle.set_output(result)
        return result


def _run_tool_sync_gen(func, args, kwargs, name, tool_type, fail_open):
    if not _check_initialized(fail_open):
        yield from func(*args, **kwargs)
        return
    tracer = _get_tracer()
    if get_current_context() is None:
        if fail_open:
            yield from func(*args, **kwargs)
            return
        raise RuntimeError("@tool: requires an active trace")
    func_name = name or func.__name__
    bound_input = _bind_arguments(func, args, kwargs)
    with tracer.tool(name=func_name, tool_type=tool_type, input=bound_input) as handle:
        result = list(func(*args, **kwargs))
        handle.set_output(result)
        yield from result


async def _run_tool_async_gen(func, args, kwargs, name, tool_type, fail_open):
    if not _check_initialized(fail_open):
        async for item in func(*args, **kwargs):
            yield item
        return
    tracer = _get_tracer()
    if get_current_context() is None:
        if fail_open:
            async for item in func(*args, **kwargs):
                yield item
            return
        raise RuntimeError("@tool: requires an active trace")
    func_name = name or func.__name__
    bound_input = _bind_arguments(func, args, kwargs)
    collected = []
    with tracer.tool(name=func_name, tool_type=tool_type, input=bound_input) as handle:
        async for item in func(*args, **kwargs):
            collected.append(item)
            yield item
        handle.set_output(collected)


# ── LLM decorator ──

def llm(name: Optional[str] = None, model: Optional[str] = None, fail_open: bool = True, **kwargs):
    """Decorator that creates a logical LLM span.

    Sets logical_llm_span_active=True so the OpenAI instrumentor does NOT
    create a second LLM span (dedup). The provider request still creates a
    GATEWAY span via the proxy.
    """
    def decorator(func):
        if _is_async(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await _run_llm_async(func, args, kwargs, name, model, fail_open)
            return async_wrapper

        if _is_async_generator(func):
            @functools.wraps(func)
            async def async_gen_wrapper(*args, **kwargs):
                async for item in _run_llm_async_gen(func, args, kwargs, name, model, fail_open):
                    yield item
            return async_gen_wrapper

        if _is_sync_generator(func):
            @functools.wraps(func)
            def sync_gen_wrapper(*args, **kwargs):
                yield from _run_llm_sync_gen(func, args, kwargs, name, model, fail_open)
            return sync_gen_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            return _run_llm_sync(func, args, kwargs, name, model, fail_open)
        return sync_wrapper

    return decorator


def _create_llm_span(func_name, model, fail_open):
    """Create a logical LLM span. Returns (span, token, tracer)."""
    if not _check_initialized(fail_open):
        return None, None, None
    tracer = _get_tracer()
    current = get_current_context()
    if current is None:
        if fail_open:
            logger.warning("@llm: no active trace — running without observation")
            return None, None, tracer
        raise RuntimeError("@llm: requires an active trace")

    span_id = generate_span_id()
    llm_ctx = SpanContext(
        trace_id=current.trace_id,
        span_id=span_id,
        parent_span_id=current.span_id,
        span_kind=SpanKind.LLM,
        sampled=current.sampled,
        logical_llm_span_active=True,
    )
    token = set_context(llm_ctx)

    span = Span(
        trace_id=current.trace_id,
        span_id=span_id,
        parent_span_id=current.span_id,
        span_name=f"llm.{func_name}",
        span_kind=SpanKind.LLM,
    )
    span.set_attribute("gen_ai.operation.name", "chat")
    if model:
        span.set_attribute("gen_ai.request.model", model)
    _apply_association(span)
    span.start()

    try:
        from .span_registry import register_span_event_sink
        register_span_event_sink(span)
    except Exception:
        pass

    return span, token, tracer


def _finalize_llm_span(span, token, tracer, exc_type, exc_val, exc_tb):
    if span is None:
        if token is not None:
            reset_context(token)
        return
    try:
        try:
            if exc_type is not None and not _control_flow_exception(exc_type, exc_val):
                span.set_error(
                    error_type=exc_type.__name__,
                    error_message=_safe_tool_error_message(exc_val) if exc_val else "",
                )
            else:
                span.set_status("OK")
            span.end()
            ctx = get_current_context()
            if ctx is not None and ctx.sampled:
                tracer.reporter.report(span.to_record())
        except Exception:
            logger.exception("LLM decorator finalization failed")
    finally:
        try:
            from .span_registry import unregister_span_event_sink
            unregister_span_event_sink(span.trace_id, span.span_id)
        except Exception:
            pass
        if token is not None:
            reset_context(token)


def _run_llm_sync(func, args, kwargs, name, model, fail_open):
    import sys as _sys
    func_name = name or func.__name__
    span, token, tracer = _create_llm_span(func_name, model, fail_open)
    if span is None and token is None:
        return func(*args, **kwargs)
    try:
        result = func(*args, **kwargs)
        return result
    except BaseException:
        raise
    finally:
        _finalize_llm_span(span, token, tracer, *_sys.exc_info())


async def _run_llm_async(func, args, kwargs, name, model, fail_open):
    import sys as _sys
    func_name = name or func.__name__
    span, token, tracer = _create_llm_span(func_name, model, fail_open)
    if span is None and token is None:
        return await func(*args, **kwargs)
    try:
        result = await func(*args, **kwargs)
        return result
    except BaseException:
        raise
    finally:
        _finalize_llm_span(span, token, tracer, *_sys.exc_info())


def _run_llm_sync_gen(func, args, kwargs, name, model, fail_open):
    import sys as _sys
    func_name = name or func.__name__
    span, token, tracer = _create_llm_span(func_name, model, fail_open)
    if span is None and token is None:
        yield from func(*args, **kwargs)
        return
    try:
        yield from func(*args, **kwargs)
    except BaseException:
        raise
    finally:
        _finalize_llm_span(span, token, tracer, *_sys.exc_info())


async def _run_llm_async_gen(func, args, kwargs, name, model, fail_open):
    import sys as _sys
    func_name = name or func.__name__
    span, token, tracer = _create_llm_span(func_name, model, fail_open)
    if span is None and token is None:
        async for item in func(*args, **kwargs):
            yield item
        return
    try:
        async for item in func(*args, **kwargs):
            yield item
    except BaseException:
        raise
    finally:
        _finalize_llm_span(span, token, tracer, *_sys.exc_info())
