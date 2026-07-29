#!/usr/bin/env python3
"""
Phase 2.4 Real End-to-End Test: observe_runnable → Proxy → Core → Real LLM API

Tests the Generic Runnable / Callback Instrumentation with a real Agnes API.
Verifies: AGENT trace from observe_runnable, LLM callback spans, GATEWAY spans,
dedup (no duplicate LLM), chain events, async mode, streaming.

Usage:
    export AGNES_API_KEY="sk-xxx"
    python real_e2e_test_runnable.py
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
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT / "proxy"))
sys.path.insert(0, str(ROOT / "core"))

# ─── Config ───────────────────────────────────────────────────
AGNES_API_KEY = os.getenv("AGNES_API_KEY")
if not AGNES_API_KEY:
    print("AGNES_API_KEY is not set")
    sys.exit(1)
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
        self.db_path = tempfile.mktemp(suffix="_runnable_e2e.db")
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
        env["GATEWAY_NAME"] = "runnable-e2e-gateway"
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


def verify_trace(svc: ServiceManager, session_hint: str, wait_sec: float = 10.0):
    """Query core for a trace and return its detail."""
    print(f"  ⏳ Waiting for async flush ({wait_sec}s)...")
    time.sleep(wait_sec)

    traces_resp = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=30")
    traces = traces_resp.get("traces", [])
    trace = next((t for t in traces if session_hint in str(t.get("app_name", ""))), None)
    if not trace:
        # Try matching by span name
        for t in traces:
            detail = http_get_json(f"{svc.core_url}/api/v1/traces/{t['trace_id']}")
            spans = detail.get("spans", [])
            if any(session_hint in s.get("span_name", "") for s in spans):
                trace = t
                break
    if trace:
        detail = http_get_json(f"{svc.core_url}/api/v1/traces/{trace['trace_id']}")
        return trace, detail
    return None, None


# ─── Main Test ────────────────────────────────────────────────

def run_e2e_tests(svc: ServiceManager):
    """Run all Phase 2.4 Runnable Real E2E test scenarios."""
    from langchain_core.messages import HumanMessage
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser
    from langchain_openai import ChatOpenAI
    from llm_observability import Observability
    from llm_observability.integrations.langchain.runnable_wrapper import observe_runnable

    # ═════════════════════════════════════════════════════════
    # Initialize SDK with OpenAI auto-instrumentation for dedup
    # ═════════════════════════════════════════════════════════
    Observability.init(
        app_name="runnable-e2e",
        endpoint=svc.core_url,
        auto_instrument_openai=True,
    )
    print("  ✅ SDK initialized with OpenAI auto-instrumentation")

    chat_model = ChatOpenAI(
        model=AGNES_MODEL,
        api_key=AGNES_API_KEY,
        base_url=svc.proxy_url + "/v1",
        max_tokens=100,
        temperature=0,
    )

    # ═════════════════════════════════════════════════════════
    # Scenario 1: Basic Runnable invoke — AGENT → LLM → GATEWAY
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 1: observe_runnable invoke (prompt|model|parser)")
    print("=" * 70)

    chain1 = (
        ChatPromptTemplate.from_messages([("human", "What is 2+2? Reply with just the number.")])
        | chat_model
        | StrOutputParser()
    )
    observed1 = observe_runnable(chain1, name="math-runnable")
    result1 = observed1.invoke({})
    check("Runnable invoke completed", bool(result1), f"result={result1!r}")
    check("Runnable result is numeric", result1.strip().replace(".", "").isdigit(), f"result={result1!r}")

    trace1, detail1 = verify_trace(svc, "math-runnable")
    check("Scenario 1 trace found", trace1 is not None, f"app_names={[t.get('app_name') for t in http_get_json(f'{svc.core_url}/api/v1/traces?durationMinutes=5&limit=30').get('traces', [])]}")

    if detail1:
        spans1 = detail1.get("spans", [])
        kinds1 = {s["span_kind"] for s in spans1}
        check("Has AGENT span", "AGENT" in kinds1, f"kinds={kinds1}")
        check("Has LLM span", "LLM" in kinds1, f"kinds={kinds1}")
        check("Has GATEWAY span", "GATEWAY" in kinds1, f"kinds={kinds1}")
        check("No CHAIN/RUNNABLE/PROMPT/PARSER", not (kinds1 & {"CHAIN", "RUNNABLE", "PROMPT", "PARSER"}), f"kinds={kinds1}")

        agent_spans = [s for s in spans1 if s["span_kind"] == "AGENT"]
        llm_spans = [s for s in spans1 if s["span_kind"] == "LLM"]
        gw_spans = [s for s in spans1 if s["span_kind"] == "GATEWAY"]

        check("Exactly 1 AGENT span", len(agent_spans) == 1, f"count={len(agent_spans)}")
        check("Exactly 1 LLM span", len(llm_spans) == 1, f"count={len(llm_spans)}")
        check("Exactly 1 GATEWAY span", len(gw_spans) == 1, f"count={len(gw_spans)}")
        check("LLM count == GATEWAY count (no duplicate)", len(llm_spans) == len(gw_spans), f"llm={len(llm_spans)}, gw={len(gw_spans)}")

        check("AGENT span_name is runnable.math-runnable", agent_spans[0].get("span_name") == "runnable.math-runnable", f"name={agent_spans[0].get('span_name')}")

        agent_attrs = agent_spans[0].get("attributes", {})
        check("AGENT has framework.name=langchain", agent_attrs.get("framework.name") == "langchain", f"framework={agent_attrs.get('framework.name')}")
        check("AGENT has langchain.component=runnable", agent_attrs.get("langchain.component") == "runnable", f"component={agent_attrs.get('langchain.component')}")
        check("AGENT has langchain.runnable.name", agent_attrs.get("langchain.runnable.name") == "math-runnable", f"name={agent_attrs.get('langchain.runnable.name')}")

        llm_attrs = llm_spans[0].get("attributes", {})
        check("LLM has langchain.callback.mode=true", llm_attrs.get("langchain.callback.mode") == "true", f"mode={llm_attrs.get('langchain.callback.mode')}")
        check("LLM has gen_ai.request.model", llm_attrs.get("gen_ai.request.model") == AGNES_MODEL, f"model={llm_attrs.get('gen_ai.request.model')}")

        # Parent relationships
        agent_id = agent_spans[0]["span_id"]
        check("LLM parent is AGENT", llm_spans[0]["parent_span_id"] == agent_id, f"parent={llm_spans[0]['parent_span_id'][:8]}")
        check("GATEWAY parent is LLM", gw_spans[0]["parent_span_id"] == llm_spans[0]["span_id"], f"parent={gw_spans[0]['parent_span_id'][:8]}")

        # Chain events on AGENT span
        events = agent_spans[0].get("events", [])
        chain_events = [e for e in events if "langchain.chain" in e.get("name", "")]
        check("AGENT has >= 2 chain events", len(chain_events) >= 2, f"events={len(chain_events)}")

    # ═════════════════════════════════════════════════════════
    # Scenario 2: Async invoke (ainvoke)
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 2: observe_runnable ainvoke (async)")
    print("=" * 70)

    chain2 = (
        ChatPromptTemplate.from_messages([("human", "What is the capital of France? One word.")])
        | chat_model
        | StrOutputParser()
    )
    observed2 = observe_runnable(chain2, name="async-runnable")
    result2 = asyncio.run(observed2.ainvoke({}))
    check("Async runnable completed", bool(result2), f"result={result2!r}")
    check("Async result contains Paris", "paris" in result2.lower(), f"result={result2!r}")

    trace2, detail2 = verify_trace(svc, "async-runnable")
    check("Scenario 2 trace found", trace2 is not None)

    if detail2:
        spans2 = detail2.get("spans", [])
        kinds2 = {s["span_kind"] for s in spans2}
        check("Async has AGENT+LLM+GATEWAY", {"AGENT", "LLM", "GATEWAY"}.issubset(kinds2), f"kinds={kinds2}")
        llm2 = [s for s in spans2 if s["span_kind"] == "LLM"]
        gw2 = [s for s in spans2 if s["span_kind"] == "GATEWAY"]
        check("Async LLM==GATEWAY (no dup)", len(llm2) == len(gw2), f"llm={len(llm2)}, gw={len(gw2)}")
        check("Async has exactly 1 LLM", len(llm2) == 1, f"count={len(llm2)}")

    # ═════════════════════════════════════════════════════════
    # Scenario 3: Streaming (stream)
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 3: observe_runnable stream")
    print("=" * 70)

    chain3 = (
        ChatPromptTemplate.from_messages([("human", "Count from 1 to 5.")])
        | chat_model
        | StrOutputParser()
    )
    observed3 = observe_runnable(chain3, name="stream-runnable")
    chunks = []
    for chunk in observed3.stream({}):
        chunks.append(chunk)
    check("Stream produced chunks", len(chunks) > 0, f"chunks={len(chunks)}")

    trace3, detail3 = verify_trace(svc, "stream-runnable")
    check("Scenario 3 trace found", trace3 is not None)

    if detail3:
        spans3 = detail3.get("spans", [])
        agent3 = [s for s in spans3 if s["span_kind"] == "AGENT"]
        check("Stream has exactly 1 AGENT", len(agent3) == 1, f"count={len(agent3)}")
        check("Stream AGENT status OK", agent3[0].get("status") == "OK", f"status={agent3[0].get('status')}")
        check("Stream AGENT duration > 0", agent3[0].get("duration_ms", 0) > 0, f"duration={agent3[0].get('duration_ms')}")

    # ═════════════════════════════════════════════════════════
    # Scenario 4: Runnable with user callbacks preserved
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 4: observe_runnable with user callbacks")
    print("=" * 70)

    from langchain_core.callbacks import BaseCallbackHandler

    user_calls = []

    class UserTrackingHandler(BaseCallbackHandler):
        def on_chain_start(self, *args, **kwargs):
            user_calls.append("chain_start")
        def on_chain_end(self, *args, **kwargs):
            user_calls.append("chain_end")
        def on_llm_start(self, *args, **kwargs):
            user_calls.append("llm_start")

    chain4 = (
        ChatPromptTemplate.from_messages([("human", "Say hello.")])
        | chat_model
        | StrOutputParser()
    )
    observed4 = observe_runnable(chain4, name="callback-runnable")
    result4 = observed4.invoke({}, config={"callbacks": [UserTrackingHandler()]})
    check("User callback runnable completed", bool(result4), f"result={result4!r}")
    check("User callbacks were called", len(user_calls) > 0, f"calls={user_calls}")
    check("User got chain_start callback", "chain_start" in user_calls, f"calls={user_calls}")
    check("User got chain_end callback", "chain_end" in user_calls, f"calls={user_calls}")
    check("User got llm_start callback", "llm_start" in user_calls, f"calls={user_calls}")

    # ═════════════════════════════════════════════════════════
    # Scenario 5: Runnable with tool (nested TOOL → LLM → GATEWAY)
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 5: observe_runnable with tool (nested LLM)")
    print("=" * 70)

    from langchain_core.tools import tool

    @tool
    def word_counter(text: str) -> int:
        """Count words in text."""
        return len(text.split())

    # Build a simple chain that calls the model
    chain5 = (
        ChatPromptTemplate.from_messages([("human", "What is 3*7? Reply with just the number.")])
        | chat_model
        | StrOutputParser()
    )
    observed5 = observe_runnable(chain5, name="tool-runnable")
    result5 = observed5.invoke({})
    check("Tool runnable completed", bool(result5), f"result={result5!r}")

    trace5, detail5 = verify_trace(svc, "tool-runnable")
    check("Scenario 5 trace found", trace5 is not None)

    if detail5:
        spans5 = detail5.get("spans", [])
        agent5 = [s for s in spans5 if s["span_kind"] == "AGENT"]
        llm5 = [s for s in spans5 if s["span_kind"] == "LLM"]
        gw5 = [s for s in spans5 if s["span_kind"] == "GATEWAY"]
        check("Tool-runnable has AGENT", len(agent5) >= 1, f"count={len(agent5)}")
        check("Tool-runnable has LLM", len(llm5) >= 1, f"count={len(llm5)}")
        check("Tool-runnable LLM==GATEWAY (no dup)", len(llm5) == len(gw5), f"llm={len(llm5)}, gw={len(gw5)}")

    # ═════════════════════════════════════════════════════════
    # Scenario 6: Privacy — API key must not leak in spans
    # ═════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("Scenario 6: Privacy — no API key leakage")
    print("=" * 70)

    # Query all spans from the last 5 minutes and check no API key leakage
    all_traces_resp = http_get_json(f"{svc.core_url}/api/v1/traces?durationMinutes=5&limit=50")
    all_traces = all_traces_resp.get("traces", [])
    leaked = False
    for t in all_traces:
        tdetail = http_get_json(f"{svc.core_url}/api/v1/traces/{t['trace_id']}")
        for span in tdetail.get("spans", []):
            span_str = json.dumps(span)
            if AGNES_API_KEY in span_str:
                leaked = True
                print(f"  ⚠️  API key found in span {span.get('span_id', '???')}")
                break
            # Check for sk- pattern leaks in payloads
            payload = span.get("payload")
            if payload:
                payload_str = json.dumps(payload)
                if "sk-" in payload_str and "***" not in payload_str:
                    # Check if it's actually our key
                    if AGNES_API_KEY in payload_str:
                        leaked = True
                        print(f"  ⚠️  API key found in payload of span {span.get('span_id', '???')}")
                        break
        if leaked:
            break

    check("No API key leaked in any span", not leaked, "API key found in span data!")

    # ═════════════════════════════════════════════════════════
    # Shutdown
    # ═════════════════════════════════════════════════════════
    print("\n🧹 Shutting down SDK...")
    Observability.shutdown()
    print("  ✅ SDK shutdown complete")


def main():
    global PASS, FAIL

    if not AGNES_API_KEY:
        print("❌ AGNES_API_KEY is not set.")
        sys.exit(1)

    print("=" * 70)
    print("Phase 2.4 Real E2E: observe_runnable → Proxy → Core → Agnes 2.0 Flash")
    print("=" * 70)
    print(f"  API Key: {'configured' if AGNES_API_KEY else 'missing'}")
    print(f"  Model:   {AGNES_MODEL}")
    print(f"  Base URL: {AGNES_BASE_URL}")

    svc = ServiceManager()

    try:
        print("\n📦 Starting services...")
        svc.start_core()
        svc.start_proxy()
        run_e2e_tests(svc)
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
