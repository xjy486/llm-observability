"""Phase 2.5 final closeout — Runtime config enforcement (P0-5)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import pytest
from llm_observability import Observability
from llm_observability.decorators import agent, chain, task, tool, llm


def _clean_init(**kwargs):
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="t", endpoint="http://localhost:99999",
                       auto_instrument_openai=False, **kwargs)
    return Observability._tracer


def _records():
    return list(Observability._tracer.reporter._queue)


def test_agent_respects_custom_max_payload_bytes():
    _clean_init(max_payload_bytes=2048)
    @agent()
    def qa():
        return "x" * 20000
    qa()
    rec = _records()[-1]
    assert rec["payload"] is not None
    out = rec["payload"].get("output")
    assert len(out) <= 2048 + 50  # truncated near budget
    assert rec["attributes"].get("task.output.truncated") is True
    Observability.shutdown()


def test_task_respects_custom_max_payload_bytes():
    _clean_init(max_payload_bytes=2048)
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.task(name="sub") as h:
            h.set_output("x" * 20000)
    task_recs = [r for r in _records() if r["span_kind"] == "TASK"]
    out = task_recs[0]["payload"]["output"]
    assert len(out) <= 2048 + 50
    assert task_recs[0]["attributes"].get("task.output.truncated") is True
    Observability.shutdown()


def test_tool_respects_custom_max_payload_bytes():
    _clean_init(max_payload_bytes=2048)
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.tool(name="search") as h:
            h.set_output("x" * 20000)
    tool_recs = [r for r in _records() if r["span_kind"] == "TOOL"]
    out = tool_recs[0]["payload"]["output"]
    assert len(out) <= 2048 + 50
    Observability.shutdown()


def test_task_attribute_respects_custom_max_attribute_bytes():
    _clean_init(max_attribute_bytes=1024)
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        with tracer.task(name="sub", attributes={"big": "v" * 5000}) as h:
            h.set_output("ok")
    task_recs = [r for r in _records() if r["span_kind"] == "TASK"]
    big_attr = task_recs[0]["attributes"].get("big", "")
    assert len(str(big_attr)) <= 1100
    Observability.shutdown()


def test_agent_default_fail_open_uses_global_false():
    _clean_init(fail_open=False)
    # No active trace + fail_open defaults to None -> resolves to global False
    @chain()
    def sub():
        return 1
    with pytest.raises(RuntimeError):
        sub()
    Observability.shutdown()


def test_decorator_explicit_fail_open_overrides_global():
    _clean_init(fail_open=False)
    # Explicit fail_open=True overrides global False
    @chain(fail_open=True)
    def sub():
        return 1
    assert sub() == 1  # runs without observation (no trace)
    assert len(_records()) == 0
    Observability.shutdown()


def test_max_payload_bytes_upper_bound_validation():
    if Observability._initialized:
        Observability.shutdown()
    with pytest.raises(ValueError):
        Observability.init(app_name="x", endpoint="http://localhost:1",
                           auto_instrument_openai=False, max_payload_bytes=20 * 1024 * 1024)


def test_stream_accumulator_uses_custom_max_payload_bytes():
    _clean_init(max_payload_bytes=2048)
    @agent()
    def outer():
        @chain()
        def gen():
            for i in range(100):
                yield "x" * 200
        return list(gen())
    outer()
    task_recs = [r for r in _records() if r["span_kind"] == "TASK"]
    assert task_recs[0]["attributes"].get("task.output.truncated") is True
    Observability.shutdown()


def test_annotate_respects_custom_max_payload_bytes():
    _clean_init(max_payload_bytes=2048)
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        Observability.annotate(output_data="x" * 20000)
    rec = _records()[-1]
    out = rec["payload"]["output"]
    assert len(out) <= 2048 + 50
    assert rec["attributes"].get("sdk.annotation.output.truncated") is True
    Observability.shutdown()
