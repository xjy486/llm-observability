# LLM Observability Platform — Project Context

## Architecture
- Telemetry Proxy: Python + aiohttp, async reverse proxy for OpenAI-compatible API (port 8082)
- Observability Core: Python + FastAPI, SQLite for MVP storage (port 8001)
- Web UI: React + TypeScript + Vite + TailwindCSS (port 3000)

## Key Principles (from PRD)
- Platform is LLM-provider-agnostic — proxy forwards to any OpenAI-compatible upstream
- Trace: inherit W3C traceparent if present, else auto-create; inject traceparent to downstream
- SessionID/UserID for filtering only, NEVER for trace merging
- Payload and Span metadata are logically separated (payload_ref)
- Telemetry failures must NOT block main LLM requests
- Payload strategies: OFF / Metadata Only / Masked / Full
- Streaming responses aggregated by StreamingAccumulator (incremental, no raw chunk storage; `capture_payload=False` mode skips content/reasoning/tool_call caching for memory efficiency)

## Key Metrics (P0-NEW-02 timing semantics)
- `duration_ms`    = Total request latency (always set)
- `first_chunk_ms` = Time to first SSE chunk (streaming only; NULL for non-streaming)
- `ttft_ms`        = Time to first meaningful token (streaming only; NULL for non-streaming)
- `ttfc_ms` has been REMOVED — it was redundant with `duration_ms`
- Dashboard metrics separated into three layers: Trace (trace_count, error_rate), LLM Call (latency, TTFT, first_chunk_ms, tokens), Span (span_count)
- Pagination total uses independent COUNT query, not len(results)
- TimeSeries metrics use the same semantics as Summary (trace_error_count, llm_error_count, llm_avg_latency_ms)
- Trace filters: status/duration are trace-level; model is span-level EXISTS subquery (returns complete traces)

## Code Standards
- Python: 4-space indentation, type hints on public functions, Google-style docstrings
- TypeScript: 2-space indentation, explicit types
- All API endpoints return JSON

## Key Commands
- Python env: `uv venv .venv && uv pip install -r core/requirements.txt -r proxy/requirements.txt` (or `pip install -r ...` in an active venv); activate with `source .venv/bin/activate`. Run services with the `.venv` interpreter (e.g. `.venv/bin/python`, `.venv/bin/uvicorn`).
- Proxy: `cd proxy && python main.py` (port 8082, env: UPSTREAM_URL, OBSERVABILITY_ENDPOINT, PAYLOAD_STRATEGY, GATEWAY_NAME, MASK_KEYS)
- Core: `cd core && uvicorn api.main:app --port 8001` (env: DB_PATH)
- UI: `cd ui && npm run dev` (port 3000)

## Data Model
- Trace: trace_id, root_span_id, start/end_time, duration_ms, session_id, user_id, status
- Span: span_id, parent_span_id, span_name, span_kind (LLM/AGENT/TOOL/GATEWAY), start/end_time, status, attributes (gen_ai.*), events, ttft_ms, first_chunk_ms, payload_ref
- Payload: request, response, request_metadata (stored separately, masked per strategy)
- db.get_trace_summaries() returns {"traces": [...], "total": int} dict

## Current Status
- Active development: **Phase 3 — Gateway Native Observability** rework is complete; the gateway-observability contract + runtime are frozen in `openspec/specs/gateway-observability-{contract,runtime}/`. Source under `sdk/python/llm_observability/gateway_observability/` (17 modules: runtime, router_span, attempt_span, streaming, recorder, privacy, cost, usage, events, context, registry, propagation, adapter, attributes, errors, aggregation, `__init__`).
- Phases 2.1–2.5 (SDK & Agent Trace / Tool Span / LangChain Auto-Instr / Runnable Callback / SDK parity) are **COMPLETE / FROZEN**.
- Test suite: ~950+ tests pass (SDK + proxy/core + gateway observability). Run: `.venv/bin/python -m pytest sdk/tests/ tests/ -q`.
- OpenSpec changes live under `openspec/changes/` (active) and `openspec/changes/archive/` (frozen history — never edit).
- Repo: https://github.com/xjy486/llm-observability

## Phase History (detailed specs in docs/)
| Phase | Spec / Closeout doc (glob) | Status |
|-------|----------------------------|--------|
| Foundation Fix (P1+P2) | docs/llm-observability-{fix,second-round-fix}-requirements.md | done |
| 2.1 SDK & Agent Trace | docs/application-sdk-agent-trace-development-spec.md, docs/llm-observability-phase2.1-*.md | COMPLETE / FROZEN |
| 2.2 Tool Span | docs/llm-observability-phase2.2-*.md | done |
| 2.3 LangChain Auto-Instr | docs/llm-observability-phase2.3-*.md | done |
| 2.4 Runnable Callback | docs/llm-observability-phase2.4-generic-runnable-callback-development-spec.md | done |
| 2.5 SDK Parity | docs/llm-observability-phase2.5-agentlens-sdk-parity-development-spec.md, docs/llm-observability-phase2.5-*.md | done |
| 3 Gateway Native Obs | docs/llm-observability-phase3-*.md; frozen specs in openspec/specs/gateway-observability-{contract,runtime}/ | rework complete |
- PRD: docs/LLM_Agent_Observability_PRD_v0.1.md
