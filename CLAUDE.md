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
- Proxy: `cd proxy && python main.py` (port 8082, env: UPSTREAM_URL, OBSERVABILITY_ENDPOINT, PAYLOAD_STRATEGY, GATEWAY_NAME, MASK_KEYS)
- Core: `cd core && uvicorn api.main:app --port 8001` (env: DB_PATH)
- UI: `cd ui && npm run dev` (port 3000)

## Data Model
- Trace: trace_id, root_span_id, start/end_time, duration_ms, session_id, user_id, status
- Span: span_id, parent_span_id, span_name, span_kind (LLM/AGENT/TOOL/GATEWAY), start/end_time, status, attributes (gen_ai.*), events, ttft_ms, first_chunk_ms, payload_ref
- Payload: request, response, request_metadata (stored separately, masked per strategy)
- db.get_trace_summaries() returns {"traces": [...], "total": int} dict

## Current Status — Foundation Fix Phase 2 complete
- Phase 1 + Phase 2 (Foundation Fix) complete — all P0/P1 second-round fixes applied
- API contract aligned: Backend response fields match Frontend TypeScript types
- Timing semantics: `duration_ms`, `first_chunk_ms`, `ttft_ms` (NULL for non-streaming); `ttfc_ms` removed
- Trace filters: status/duration at trace level, model at span level (EXISTS subquery, returns complete traces)
- SQLite schema migration: ALTER TABLE + metadata version table, old DBs auto-upgrade
- TimeSeries metrics consistent with Summary (trace_error_count, llm_error_count, llm_avg_latency_ms)
- MASK_KEYS env var merged with defaults (never overrides base security keys)
- StreamingAccumulator supports `capture_payload=False` for memory-efficient mode
- Tests: contract tests + filter semantics tests pass
- Pushed to https://github.com/xjy486/llm-observability
