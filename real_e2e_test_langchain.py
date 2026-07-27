#!/usr/bin/env python3
"""
Phase 2.3 Real End-to-End Test: LangChain Agent → Proxy → Core → Real LLM API

Architecture:
    ObservedLangChainAgent (AGENT span)
      → LangChainObservabilityMiddleware creates LLM + TOOL child spans
      → ChatOpenAI (OpenAI Instrumentor dedup via logical_llm_span_active)
      → injects traceparent + ownership marker headers
      → sends request to Proxy
    Proxy (GATEWAY span)
      → forwards to upstream LLM API (Agnes 2.0 Flash)
      → captures timing, tokens, payload
      → reports GATEWAY span to Core
    Core (FastAPI + SQLite)
      → stores all spans
      → provides query APIs

Usage:
    export AGNES_API_KEY="sk-xxx"
    python real_e2e_test_langchain.py
"""
import sys
import os
import time
import json
import socket
import asyncio
import subprocess
import tempfile
from pathlib import Path

import urllib.request
import urllib.error

# ─── Path Setup ──────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT / "proxy"))
sys.path.insert(0, str(ROOT / "core"))

# ─── Config ───────────────────────────────────────────────────
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")
AGNES_BASE_URL = "https://apihub.agnes-ai.com"
AGNES_MODEL = "agnes-2.0-flash"


# ─── Helpers ──────────────────────────────────────────────────

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(url: str, timeout: float = 15.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(interval)
    return False


def http_get_json(url: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─── Service Management ───────────────────────────────────────

class ServiceManager:
    def __init__(self):
        self.core_port = find_free_port()
        self.proxy_port = find_free_port()
        self.db_path = tempfile.mktemp(suffix="_langchain_e2e.db")
        self.core_proc = None
        self.proxy_proc = None

    @property
    def core_url(self) -> str:
        return f"http://127.0.0.1:{self.core_port}"

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}"

    def start_core(self):
        env = os.environ.copy()
        env["DB_PATH"] = self.db_path
        env["PYTHONPATH"] = str(ROOT / "core")
        cmd = [
            sys.executable, "-m", "uvicorn",
            "api.main:app",
            "--host", "127.0.0.1",
            "--port", str(self.core_port),
        ]
        self.core_proc = subprocess.Popen(
            cmd, cwd=str(ROOT / "core"), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if not wait_for_server(f"{self.core_url}/api/v1/health"):
            out, err = self.core_proc.communicate(timeout=5)
            raise RuntimeError(
                f"Core failed to start on port {self.core_port}\n"
                f"stderr: {err.decode()[:2000]}"
            )
        print(f"  ✅ Core started on port {self.core_port}")

    def start_proxy(self):
        env = os.environ.copy()
        env["PROXY_HOST"] = "127.0.0.1"
        env["PROXY_PORT"] = str(self.proxy_port)
        env["UPSTREAM_URL"] = AGNES_BASE_URL
        env["OBSERVABILITY_ENDPOINT"] = self.core_url
        env["PAYLOAD_STRATEGY"] = "masked"
        env["GATEWAY_NAME"] = "langchain-e2e-gateway"
        cmd = [sys.executable, "main.py"]
        self.proxy_proc = subprocess.Popen(
            cmd, cwd=str(ROOT / "proxy"), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if not wait_for_server(f"{self.proxy_url}/health"):
            out, err = self.proxy_proc.communicate(timeout=5)
            raise RuntimeError(
                f"Proxy failed to start on port {self.proxy_port}\n"
                f"stderr: {err.decode()[:2000]}"
            )
        print(f"  ✅ Proxy started on port {self.proxy_port}")

    def stop_all(self):
        for proc, name in [(self.proxy_proc, "Proxy"), (self.core_proc, "Core")]:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                print(f"  🛑 {name} stopped")
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)


# ─── Test Results Tracker ─────────────────────────────────────

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


# ─── Main Test ────────────────────────────────────────────────

async def run_e2e_tests(svc: ServiceManager):
    """Run all LangChain Real E2E test scenarios."""
    from langchain.agents import create_agent
    from langchain_core.tools import tool
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    from llm_observability import Observability
    from llm_observability.integrations.langchain import (
        observe_agent,
        LangChainObservabilityMiddleware,
    )

    # ═════════════════════════════════════════════════════════
    # Initialize SDK — auto_instrument_openai=True enables
    # OpenAI Instrumentor dedup via logical_llm_span_active.
    # ═════════════════════════════════════════════════════════
    Observability.init(
        app_name="langchain-e2e",
        endpoint=svc.core_url,
        auto_instrument_openai=True,
    )
    print("  ✅ SDK initialized with OpenAI auto-instrumentation")

    # ChatOpenAI pointing to Proxy — Proxy forwards to Agnes upstream
    chat_model = ChatOpenAI(
        model=AGNES_MODEL,
        api_key=AGNES_API_KEY,
        base_url=svc.proxy_url + "/v1",
        max_tokens=100,
        temperature=0,
    )

    @tool
    def calculator(x: int, y: int) -> int:
        """Add two integers and return the sum."""
        return x + y

    # ═════════════════════════════════════════════════════════
    # Scenario 1: Full Agent Loop — AGENT → LLM → GATEWAY + TOOL → LLM → GATEWAY
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 1: Full LangChain Agent Loop (invoke)")
    print("=" * 70)

    agent = create_agent(
        model=chat_model,
        tools=[calculator],
        middleware=[LangChainObservabilityMiddleware()],
    )
    observed = observe_agent(
        agent,
        name="math-agent",
        session_id="langchain-e2e-1",
        user_id="langchain-e2e-user-1",
        business_scene="testing",
    )

    result = observed.invoke({
        "messages": [HumanMessage(content="What is 7+5? Use the calculator tool.")],
    })

    final_messages = result.get("messages", [])
    check("Agent loop completed", len(final_messages) >= 1, f"msg_count={len(final_messages)}")
    final_text = ""
    for m in reversed(final_messages):
        content = getattr(m, "content", "")
        if content and not getattr(m, "tool_calls", None):
            final_text = content
            break
    check("Agent produced final answer", bool(final_text), f"final={final_text[:80]!r}")

    print("  ⏳ Waiting for async flush (10s)...")
    await asyncio.sleep(10)

    # ─── Query Core ───
    traces_resp = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=20")
    traces = traces_resp.get("traces", [])
    trace = next((t for t in traces if t.get("session_id") == "langchain-e2e-1"), None)
    check("Trace found by session_id", trace is not None, f"sessions={[t.get('session_id') for t in traces]}")

    if trace:
        trace_id = trace["trace_id"]
        check("Trace app_name is langchain-e2e", trace.get("app_name") == "langchain-e2e", f"app={trace.get('app_name')}")
        check("Trace has business_scene=testing", trace.get("business_scene") == "testing", f"scene={trace.get('business_scene')}")
        check("Trace status is OK", trace.get("status") == "OK", f"status={trace.get('status')}")
        check("Trace llm_call_count >= 1", trace.get("llm_call_count", 0) >= 1, f"llm_calls={trace.get('llm_call_count')}")
        check("Trace total_tokens > 0", trace.get("total_tokens", 0) > 0, f"tokens={trace.get('total_tokens')}")

        detail = http_get_json(f"{svc.core_url}/api/v1/traces/{trace_id}")
        spans = detail.get("spans", [])
        span_kinds = {s["span_kind"] for s in spans}
        check("Has AGENT span", "AGENT" in span_kinds, f"kinds={span_kinds}")
        check("Has LLM span(s)", "LLM" in span_kinds, f"kinds={span_kinds}")
        check("Has GATEWAY span(s)", "GATEWAY" in span_kinds, f"kinds={span_kinds}")

        agent_spans = [s for s in spans if s["span_kind"] == "AGENT"]
        llm_spans = [s for s in spans if s["span_kind"] == "LLM"]
        tool_spans = [s for s in spans if s["span_kind"] == "TOOL"]
        gw_spans = [s for s in spans if s["span_kind"] == "GATEWAY"]

        check("Exactly 1 AGENT span", len(agent_spans) == 1, f"count={len(agent_spans)}")
        check("At least 1 LLM span", len(llm_spans) >= 1, f"count={len(llm_spans)}")
        check("At least 1 GATEWAY span", len(gw_spans) >= 1, f"count={len(gw_spans)}")
        check("AGENT span_name is agent.math-agent", agent_spans[0].get("span_name") == "agent.math-agent", f"name={agent_spans[0].get('span_name')}")

        # ─── Critical: NO duplicate LLM spans (dedup) ───
        # Each LLM call produces exactly 1 LLM span + 1 GATEWAY span.
        check(
            "LLM count == GATEWAY count (no duplicate LLM)",
            len(llm_spans) == len(gw_spans),
            f"llm={len(llm_spans)}, gw={len(gw_spans)}",
        )

        # ─── Parent relationships ───
        agent_span_id = agent_spans[0]["span_id"]
        for s in llm_spans:
            check(
                f"LLM span {s['span_id'][:8]} parent is AGENT",
                s["parent_span_id"] == agent_span_id,
                f"parent={s['parent_span_id'][:8]}, agent={agent_span_id[:8]}",
            )
        for s in tool_spans:
            check(
                f"TOOL span {s['span_id'][:8]} parent is AGENT",
                s["parent_span_id"] == agent_span_id,
                f"parent={s['parent_span_id'][:8]}, agent={agent_span_id[:8]}",
            )
        # Each GATEWAY's parent must be the corresponding LLM span
        for gw in gw_spans:
            gw_parent = gw["parent_span_id"]
            parent_is_llm = any(gw_parent == llm["span_id"] for llm in llm_spans)
            check(
                f"GATEWAY {gw['span_id'][:8]} parent is an LLM span",
                parent_is_llm,
                f"gw_parent={gw_parent[:8]}",
            )

        # ─── LLM attributes ───
        for llm in llm_spans:
            attrs = llm.get("attributes", {})
            check(
                f"LLM {llm['span_id'][:8]} has gen_ai.request.model",
                attrs.get("gen_ai.request.model") == AGNES_MODEL,
                f"model={attrs.get('gen_ai.request.model')}",
            )
            check(
                f"LLM {llm['span_id'][:8]} has framework.name=langchain",
                attrs.get("framework.name") == "langchain",
                f"framework={attrs.get('framework.name')}",
            )

        # ─── GATEWAY attributes ───
        for gw in gw_spans:
            check(f"GATEWAY {gw['span_id'][:8]} status OK", gw.get("status") == "OK", f"status={gw.get('status')}")
            check(f"GATEWAY {gw['span_id'][:8]} duration > 0", gw.get("duration_ms", 0) > 0, f"duration={gw.get('duration_ms')}")
            gw_attrs = gw.get("attributes", {})
            check(
                f"GATEWAY {gw['span_id'][:8]} has token usage",
                gw_attrs.get("gen_ai.usage.total_tokens", 0) > 0,
                f"tokens={gw_attrs.get('gen_ai.usage.total_tokens')}",
            )

        # ─── TOOL span (if tool was called) ───
        if tool_spans:
            ts = tool_spans[0]
            check("TOOL span_name is tool.calculator", ts.get("span_name") == "tool.calculator", f"name={ts.get('span_name')}")
            t_attrs = ts.get("attributes", {})
            check("TOOL has tool.name=calculator", t_attrs.get("tool.name") == "calculator", f"name={t_attrs.get('tool.name')}")
            check("TOOL has framework.name=langchain", t_attrs.get("framework.name") == "langchain", f"framework={t_attrs.get('framework.name')}")
            print(f"  ℹ️  TOOL span detected: calculator was called by the agent")

    # ═════════════════════════════════════════════════════════
    # Scenario 2: Streaming Agent — trace lifecycle covers full iteration
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 2: Streaming LangChain Agent (stream)")
    print("=" * 70)

    agent2 = create_agent(
        model=chat_model,
        tools=[calculator],
        middleware=[LangChainObservabilityMiddleware()],
    )
    observed2 = observe_agent(
        agent2,
        name="stream-agent",
        session_id="langchain-e2e-2",
        user_id="langchain-e2e-user-2",
    )

    chunk_count = 0
    for chunk in observed2.stream({
        "messages": [HumanMessage(content="What is 9+1? Use the calculator tool.")],
    }):
        chunk_count += 1

    check("Streaming produced chunks", chunk_count > 0, f"chunks={chunk_count}")

    print("  ⏳ Waiting for async flush (10s)...")
    await asyncio.sleep(10)

    traces_resp2 = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=30")
    traces2 = traces_resp2.get("traces", [])
    trace2 = next((t for t in traces2 if t.get("session_id") == "langchain-e2e-2"), None)
    check("Streaming trace found", trace2 is not None, f"sessions={[t.get('session_id') for t in traces2]}")

    if trace2:
        check("Streaming trace status OK", trace2.get("status") == "OK", f"status={trace2.get('status')}")
        detail2 = http_get_json(f"{svc.core_url}/api/v1/traces/{trace2['trace_id']}")
        spans2 = detail2.get("spans", [])
        agent_spans2 = [s for s in spans2 if s["span_kind"] == "AGENT"]
        check("Streaming trace has exactly 1 AGENT span", len(agent_spans2) == 1, f"count={len(agent_spans2)}")
        check("Streaming AGENT span status OK", agent_spans2[0].get("status") == "OK", f"status={agent_spans2[0].get('status')}")
        # Trace lifecycle: AGENT span duration should cover full iteration
        check(
            "Streaming AGENT duration > 0 (covers full iteration)",
            agent_spans2[0].get("duration_ms", 0) > 0,
            f"duration={agent_spans2[0].get('duration_ms')}",
        )

    # ═════════════════════════════════════════════════════════
    # Scenario 3: Simple agent without tool — AGENT → LLM → GATEWAY
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 3: Simple Agent (no tool) — AGENT → LLM → GATEWAY")
    print("=" * 70)

    agent3 = create_agent(
        model=chat_model,
        tools=[],  # no tools
        middleware=[LangChainObservabilityMiddleware()],
    )
    observed3 = observe_agent(
        agent3,
        name="simple-agent",
        session_id="langchain-e2e-3",
        user_id="langchain-e2e-user-3",
    )

    result3 = observed3.invoke({
        "messages": [HumanMessage(content="What is 2+2? Reply with just the number.")],
    })

    check("Simple agent completed", result3 is not None)

    print("  ⏳ Waiting for async flush (10s)...")
    await asyncio.sleep(10)

    traces_resp3 = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=30")
    traces3 = traces_resp3.get("traces", [])
    trace3 = next((t for t in traces3 if t.get("session_id") == "langchain-e2e-3"), None)
    check("Simple agent trace found", trace3 is not None)

    if trace3:
        detail3 = http_get_json(f"{svc.core_url}/api/v1/traces/{trace3['trace_id']}")
        spans3 = detail3.get("spans", [])
        kinds3 = {s["span_kind"] for s in spans3}
        check("Simple trace has AGENT+LLM+GATEWAY", {"AGENT", "LLM", "GATEWAY"}.issubset(kinds3), f"kinds={kinds3}")
        llm3 = [s for s in spans3 if s["span_kind"] == "LLM"]
        gw3 = [s for s in spans3 if s["span_kind"] == "GATEWAY"]
        check("Simple trace has 1 LLM span", len(llm3) == 1, f"count={len(llm3)}")
        check("Simple trace has 1 GATEWAY span", len(gw3) == 1, f"count={len(gw3)}")
        check("Simple trace LLM==GATEWAY (no duplicate)", len(llm3) == len(gw3), f"llm={len(llm3)}, gw={len(gw3)}")

    # ═════════════════════════════════════════════════════════
    # Shutdown
    # ═════════════════════════════════════════════════════════
    print("\n🧹 Shutting down SDK...")
    Observability.shutdown()
    print("  ✅ SDK shutdown complete")


def main():
    global PASS, FAIL

    if not AGNES_API_KEY:
        print("❌ AGNES_API_KEY environment variable is not set.")
        print("   Usage: export AGNES_API_KEY='sk-xxx' && python real_e2e_test_langchain.py")
        sys.exit(1)

    print("=" * 70)
    print("Phase 2.3 Real E2E: LangChain Agent → Proxy → Core → Agnes 2.0 Flash")
    print("=" * 70)
    print(f"  API Key: {AGNES_API_KEY[:10]}...{AGNES_API_KEY[-4:]}")
    print(f"  Model:   {AGNES_MODEL}")
    print(f"  Base URL: {AGNES_BASE_URL}")

    svc = ServiceManager()

    try:
        print("\n📦 Starting services...")
        svc.start_core()
        svc.start_proxy()
        asyncio.run(run_e2e_tests(svc))
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Test failed with exception: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🧹 Cleaning up services...")
        svc.stop_all()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
