"""Inject demo telemetry data into the Core API for preview."""
import json
import time
import requests
import uuid
import random

API = "http://localhost:8001/api/v1"

now = time.time()

def gen_id():
    return uuid.uuid4().hex[:16]

traces = []

# Scenario B: SDK trace with AGENT → LLM → GATEWAY
for i in range(8):
    trace_id = gen_id()
    agent_span_id = gen_id()
    llm_span_id = gen_id()
    gateway_span_id = gen_id()

    start = now - random.randint(60, 3600)
    agent_dur = random.uniform(800, 3000)
    llm_start = start + 50
    llm_dur = random.uniform(500, 2500)
    gw_start = llm_start + 100
    gw_dur = random.uniform(400, 2000)
    is_error = i == 7  # last one is error
    status = "ERROR" if is_error else "OK"
    model = random.choice(["gpt-4", "gpt-4o", "gpt-3.5-turbo", "claude-3-sonnet"])
    in_tok = random.randint(50, 500)
    out_tok = random.randint(20, 300)

    records = [
        {
            "trace_id": trace_id,
            "span_id": agent_span_id,
            "parent_span_id": None,
            "trace_inherited": False,
            "span_name": "agent.demo-task",
            "span_kind": "AGENT",
            "start_time": start,
            "end_time": start + agent_dur / 1000,
            "duration_ms": agent_dur,
            "status": status,
            "session_id": f"sess-{i}",
            "user_id": f"user-{i % 3}",
            "app_name": "demo-app",
            "business_scene": "chatbot",
            "attributes": {"agent.framework": "langchain"},
        },
        {
            "trace_id": trace_id,
            "span_id": llm_span_id,
            "parent_span_id": agent_span_id,
            "trace_inherited": False,
            "span_name": "llm.completion",
            "span_kind": "LLM",
            "start_time": llm_start,
            "end_time": llm_start + llm_dur / 1000,
            "duration_ms": llm_dur,
            "status": status,
            "session_id": f"sess-{i}",
            "user_id": f"user-{i % 3}",
            "app_name": "demo-app",
            "business_scene": "chatbot",
            "attributes": {
                "gen_ai.request.model": model,
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": in_tok,
                "gen_ai.usage.output_tokens": out_tok,
                "gen_ai.usage.total_tokens": in_tok + out_tok,
            },
        },
        {
            "trace_id": trace_id,
            "span_id": gateway_span_id,
            "parent_span_id": llm_span_id,
            "trace_inherited": True,
            "span_name": "proxy.request",
            "span_kind": "GATEWAY",
            "start_time": gw_start,
            "end_time": gw_start + gw_dur / 1000,
            "duration_ms": gw_dur,
            "status": status,
            "http_status": 500 if is_error else 200,
            "ttft_ms": random.uniform(100, 800) if not is_error else None,
            "first_chunk_ms": random.uniform(50, 500) if not is_error else None,
            "is_stream": True,
            "session_id": f"sess-{i}",
            "user_id": f"user-{i % 3}",
            "app_name": "demo-app",
            "business_scene": "chatbot",
            "attributes": {
                "llm.gateway.name": "proxy",
                "gen_ai.request.model": model,
                "gen_ai.response.model": model,
                "gen_ai.usage.input_tokens": in_tok,
                "gen_ai.usage.output_tokens": out_tok,
            },
            "error_type": "upstream_error" if is_error else None,
            "error_message": "Connection timeout" if is_error else None,
            "request_metadata": {"model": model, "stream": True},
            "payload": {
                "input": {"messages": [{"role": "user", "content": f"Hello #{i}"}]},
                "output": {"choices": [{"message": {"content": "Hi there!"}}]},
            },
        },
    ]
    traces.extend(records)

# Scenario A: No SDK — proxy-only LLM trace
for i in range(3):
    trace_id = gen_id()
    llm_span_id = gen_id()
    start = now - random.randint(60, 3600)
    dur = random.uniform(300, 1500)
    model = random.choice(["gpt-4", "gpt-3.5-turbo"])
    in_tok = random.randint(30, 200)
    out_tok = random.randint(10, 150)

    traces.append({
        "trace_id": trace_id,
        "span_id": llm_span_id,
        "parent_span_id": None,
        "trace_inherited": False,
        "span_name": "llm.completion",
        "span_kind": "LLM",
        "start_time": start,
        "end_time": start + dur / 1000,
        "duration_ms": dur,
        "status": "OK",
        "session_id": f"legacy-sess-{i}",
        "user_id": f"user-{i}",
        "app_name": "legacy-app",
        "business_scene": "search",
        "attributes": {
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": in_tok,
            "gen_ai.usage.output_tokens": out_tok,
        },
        "request_metadata": {"model": model, "stream": False},
    })

resp = requests.post(f"{API}/ingest", json={"records": traces})
print(f"Ingested {len(traces)} records: {resp.json()}")
print(f"Health: {requests.get(f'{API}/health').json()}")
