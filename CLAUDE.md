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
- Streaming responses aggregated by StreamingAccumulator (incremental, no raw chunk storage)

## Key Metrics
- TTFT (time to first token) and TTFC (time to complete) are separate metrics
- Dashboard metrics separated into three layers: Trace (trace_count, error_rate), LLM Call (latency, TTFT/TTFC, tokens), Span (span_count)
- Pagination total uses independent COUNT query, not len(results)

## Code Standards
- Python: 4-space indentation, type hints on public functions, Google-style docstrings
- TypeScript: 2-space indentation, explicit types
- All API endpoints return JSON

## Key Commands
- Proxy: `cd proxy && python main.py` (port 8082, env: UPSTREAM_URL, OBSERVABILITY_ENDPOINT, PAYLOAD_STRATEGY, GATEWAY_NAME, MASK_KEYS)
- Core: `cd core && uvicorn api.main:app --port 8001` (env: DB_PATH)
- UI: `cd ui && npm run dev` (port 3000)

## Data Model
- Trace: trace_id, root_span_id, start/end_time, duration_ms, session_id, user_id, status
- Span: span_id, parent_span_id, span_name, span_kind (LLM/AGENT/TOOL/GATEWAY), start/end_time, status, attributes (gen_ai.*), events, ttft_ms, ttfc_ms, payload_ref
- Payload: request, response, request_metadata (stored separately, masked per strategy)
- db.get_trace_summaries() returns {"traces": [...], "total": int} dict

## Current Status (commit 221be4c)
- P0/P1 fixes complete, E2E verified (non-streaming + streaming + traceparent inheritance + metrics)
- Pushed to https://github.com/xjy486/llm-observability
