# LLM Observability Platform - Project Context

## Architecture
- Telemetry Proxy: Python + aiohttp, async reverse proxy for OpenAI-compatible API
- Observability Core: Python + FastAPI, SQLite for MVP storage
- Web UI: React + TypeScript + Vite + TailwindCSS

## Key Principles (from PRD)
- Platform is independent of any specific LLM Gateway (One-API is first adapter, not core)
- Trace: "business trace first, single-request trace fallback" — inherit W3C traceparent if present, else auto-create
- SessionID/UserID for filtering only, NEVER for trace merging
- Payload and Span metadata are logically separated (payload_ref)
- Telemetry failures must NOT block main LLM requests
- Payload strategies: OFF / Metadata Only / Masked / Full

## Code Standards
- Python: 4-space indentation, type hints on public functions, Google-style docstrings
- TypeScript: 2-space indentation, explicit types
- All API endpoints return JSON

## Key Commands
- Proxy: `cd proxy && python main.py` (port 8080)
- Core: `cd core && uvicorn api.main:app --port 8001`
- UI: `cd ui && npm run dev` (port 3000)

## Data Model
- Trace: trace_id, root_span_id, start/end_time, duration_ms, session_id, user_id, status
- Span: span_id, parent_span_id, span_name, span_kind (AGENT/LLM/TOOL/GATEWAY), start/end_time, status, attributes, events, payload_ref
- Payload: system, messages, tools, response (stored separately, referenced by payload_ref)
- LLM semantic attributes follow OpenTelemetry GenAI conventions (gen_ai.*)
