"""Phase 2.5 final closeout — Fail-open fault injection (P0-2).

Verifies that telemetry failures never alter business result or exception
for TASK and TOOL spans, across many failure points.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import pytest

from llm_observability import Observability


def _clean_init(**kwargs):
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(
        app_name="test-app", endpoint="http://localhost:99999",
        auto_instrument_openai=False, **kwargs,
    )
    return Observability._tracer


def _records():
    return list(Observability._tracer.reporter._queue)


def _patch_span_method(span, method_name, exc):
    """Monkeypatch a Span method to raise."""
    original = getattr(span, method_name)

    def raiser(*a, **kw):
        raise exc

    setattr(span, method_name, raiser)
    return original


# ── TASK fail-open ──

def test_task_span_end_failure_preserves_success_result():
    _clean_init()
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.task(name="sub") as h:
            h.set_output("biz-result")
            # end() will be called in __exit__; patch it to raise
            _patch_span_method(h._span, "end", RuntimeError("end fail"))
        # business result preserved — exit didn't raise
    Observability.shutdown()


def test_task_span_end_failure_preserves_business_error():
    _clean_init()
    tracer = Observability._tracer
    with pytest.raises(ValueError):
        with tracer.trace(name="root"):
            with tracer.task(name="sub") as h:
                _patch_span_method(h._span, "end", RuntimeError("end fail"))
                raise ValueError("biz error")
    # Original ValueError propagated, not RuntimeError
    Observability.shutdown()


def test_task_to_record_failure_preserves_success_result():
    _clean_init()
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.task(name="sub") as h:
            h.set_output("ok")
            _patch_span_method(h._span, "to_record", RuntimeError("record fail"))
    Observability.shutdown()


def test_task_set_error_failure_preserves_business_error():
    _clean_init()
    tracer = Observability._tracer
    with pytest.raises(ValueError):
        with tracer.trace(name="root"):
            with tracer.task(name="sub") as h:
                _patch_span_method(h._span, "set_error", RuntimeError("seterr fail"))
                raise ValueError("biz error")
    Observability.shutdown()


def test_task_reporter_failure_preserves_success_result():
    _clean_init()
    tracer = Observability._tracer
    original = tracer.reporter.report
    tracer.reporter.report = lambda r: (_ for _ in ()).throw(ConnectionError("down"))
    try:
        with tracer.trace(name="root"):
            with tracer.task(name="sub") as h:
                h.set_output("ok")
    finally:
        tracer.reporter.report = original
    Observability.shutdown()


def test_task_reset_context_failure_does_not_replace_business_error():
    """reset_context failure in TASK __exit__ does not replace business error,
    and restores the previous context (Blocker 3.2/3.3)."""
    _clean_init()
    tracer = Observability._tracer
    # Patch the ACTUAL reference used by task.py (imported at load time)
    import llm_observability.task as task_mod
    original_reset = task_mod.reset_context
    def flaky_reset(token):
        raise RuntimeError("reset fail")
    task_mod.reset_context = flaky_reset
    try:
        with pytest.raises(ValueError):
            with tracer.trace(name="root"):
                with tracer.task(name="sub"):
                    raise ValueError("biz error")
    finally:
        task_mod.reset_context = original_reset
    Observability.shutdown()


def test_tool_reset_context_failure_does_not_replace_business_error():
    """reset_context failure in TOOL __exit__ does not replace business error,
    and restores the previous context (Blocker 3.2/3.3)."""
    _clean_init()
    tracer = Observability._tracer
    import llm_observability.tool as tool_mod
    original_reset = tool_mod.reset_context
    def flaky_reset(token):
        raise RuntimeError("reset fail")
    tool_mod.reset_context = flaky_reset
    try:
        with pytest.raises(ValueError):
            with tracer.trace(name="root"):
                with tracer.tool(name="search"):
                    raise ValueError("biz error")
    finally:
        tool_mod.reset_context = original_reset
    Observability.shutdown()


# ── TOOL fail-open ──

def test_tool_span_end_failure_preserves_success_result():
    _clean_init()
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.tool(name="search") as h:
            h.set_output("result")
            _patch_span_method(h._span, "end", RuntimeError("end fail"))
    Observability.shutdown()


def test_tool_span_end_failure_preserves_business_error():
    _clean_init()
    tracer = Observability._tracer
    with pytest.raises(ValueError):
        with tracer.trace(name="root"):
            with tracer.tool(name="search") as h:
                _patch_span_method(h._span, "end", RuntimeError("end fail"))
                raise ValueError("biz error")
    Observability.shutdown()


def test_tool_to_record_failure_preserves_success_result():
    _clean_init()
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.tool(name="search") as h:
            h.set_output("ok")
            _patch_span_method(h._span, "to_record", RuntimeError("record fail"))
    Observability.shutdown()


def test_tool_set_error_failure_preserves_business_error():
    _clean_init()
    tracer = Observability._tracer
    with pytest.raises(ValueError):
        with tracer.trace(name="root"):
            with tracer.tool(name="search") as h:
                _patch_span_method(h._span, "set_error", RuntimeError("seterr fail"))
                raise ValueError("biz error")
    Observability.shutdown()


def test_tool_reporter_failure_preserves_success_result():
    _clean_init()
    tracer = Observability._tracer
    original = tracer.reporter.report
    tracer.reporter.report = lambda r: (_ for _ in ()).throw(ConnectionError("down"))
    try:
        with tracer.trace(name="root"):
            with tracer.tool(name="search") as h:
                h.set_output("ok")
    finally:
        tracer.reporter.report = original
    Observability.shutdown()


# ── Blocker 3.1: span-init failure + fail_open ──

def test_task_set_context_failure_fail_open_runs_business():
    """set_context failure + fail_open=True runs business without observation."""
    _clean_init(fail_open=True)
    tracer = Observability._tracer
    import llm_observability.task as task_mod
    original_set = task_mod.set_context
    def failing_set(ctx):
        raise RuntimeError("set_context boom")
    task_mod.set_context = failing_set
    try:
        from llm_observability.decorators import chain
        @chain()
        def sub():
            return "biz-result"
        # Inside a trace, set_context fails -> fail_open runs business
        with tracer.trace(name="root"):
            result = sub()
        assert result == "biz-result"
    finally:
        task_mod.set_context = original_set
    Observability.shutdown()


def test_task_set_context_failure_fail_closed_raises():
    """set_context failure + fail_open=False propagates the init error."""
    _clean_init(fail_open=False)
    tracer = Observability._tracer
    import llm_observability.task as task_mod
    original_set = task_mod.set_context
    def failing_set(ctx):
        raise RuntimeError("set_context boom")
    task_mod.set_context = failing_set
    try:
        from llm_observability.decorators import chain
        @chain()
        def sub():
            return "biz-result"
        with tracer.trace(name="root"):
            with pytest.raises(RuntimeError):
                sub()
    finally:
        task_mod.set_context = original_set
    Observability.shutdown()


def test_tool_set_context_failure_fail_open_runs_business():
    """set_context failure in TOOL + fail_open=True runs business."""
    _clean_init(fail_open=True)
    tracer = Observability._tracer
    import llm_observability.tool as tool_mod
    original_set = tool_mod.set_context
    def failing_set(ctx):
        raise RuntimeError("set_context boom")
    tool_mod.set_context = failing_set
    try:
        from llm_observability.decorators import tool
        @tool()
        def search():
            return "biz-result"
        with tracer.trace(name="root"):
            result = search()
        assert result == "biz-result"
    finally:
        tool_mod.set_context = original_set
    Observability.shutdown()


# ── Context restoration after telemetry failure ──

def test_task_context_restored_after_end_failure():
    _clean_init()
    tracer = Observability._tracer
    from llm_observability.context import get_current_context
    with tracer.trace(name="root"):
        root_ctx = get_current_context()
        with tracer.task(name="sub") as h:
            _patch_span_method(h._span, "end", RuntimeError("end fail"))
        # Context restored to root after TASK exit despite end failure
        assert get_current_context().span_id == root_ctx.span_id
    Observability.shutdown()


def test_tool_context_restored_after_end_failure():
    _clean_init()
    tracer = Observability._tracer
    from llm_observability.context import get_current_context
    with tracer.trace(name="root"):
        root_ctx = get_current_context()
        with tracer.tool(name="search") as h:
            _patch_span_method(h._span, "end", RuntimeError("end fail"))
        assert get_current_context().span_id == root_ctx.span_id
    Observability.shutdown()
