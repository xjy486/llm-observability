# LLM / Agent Observability Platform

An independent LLM/Agent observability platform that provides Trace/Span-level visibility into LLM calls, built on OpenTelemetry semantics.

## Architecture

```
Client / Agent
     │  OpenAI-compatible API
     ▼
Telemetry Proxy (:8082) ──────► Observability Core (:8001)
     │  transparent forward           │
     ▼                                ▼
LLM Provider (OpenAI,     Web UI (:3000)
  Agnes, Azure, etc.)     Dashboard + Trace Explorer
```

## Components

| Component | Description |
|-----------|-------------|
| **Telemetry Proxy** | Transparent reverse proxy that intercepts LLM API calls, captures telemetry (TTFT, tokens, latency, payload), and forwards to Observability Core |
| **Observability Core** | Backend service: ingestion, Trace/Span storage, metrics aggregation, query API |
| **Web UI** | React frontend: Dashboard, Trace List, Trace Detail with Span Tree |

## MVP Features (P0)

- ✅ Transparent proxy for `/v1/chat/completions` (non-streaming + SSE streaming)
- ✅ W3C Trace Context propagation (inherit upstream or auto-create)
- ✅ LLM Span: Model, Status, Latency, TTFT/TTFC, Tokens, Error
- ✅ Payload capture with OFF / Metadata Only / Masked / Full strategies
- ✅ Key-based recursive masking (`MASK_KEYS`) in addition to regex patterns
- ✅ Trace List with filtering (status, model, session, user, time range)
- ✅ Trace Detail with Span Tree
- ✅ Dashboard: Trace count, LLM call count, Span count, error rate, P50/P95/P99 latency, TTFT/TTFC, tokens
- ✅ Async telemetry reporting (never blocks main LLM request)
- ✅ Streaming response aggregation (SSE chunks → standardized OpenAI response)
- ✅ Configurable gateway name (`GATEWAY_NAME` env var)

## Quick Start

```bash
# Clone and start
docker-compose up -d

# Or run locally
cd proxy && pip install -r requirements.txt && python main.py
cd core && pip install -r requirements.txt && uvicorn api.main:app --port 8001
cd ui && npm install && npm run dev
```

## Configuration

### Proxy

| Env Var | Default | Description |
|---------|---------|-------------|
| `UPSTREAM_URL` | `http://localhost:3000` | Target LLM provider API URL |
| `OBSERVABILITY_ENDPOINT` | `http://localhost:8001` | Observability Core ingestion URL |
| `PROXY_PORT` | `8082` | Proxy listen port |
| `PAYLOAD_STRATEGY` | `masked` | `off` / `metadata_only` / `masked` / `full` |
| `GATEWAY_NAME` | `LLM Gateway` | Name shown in span attributes |
| `MASK_KEYS` | *(empty)* | Comma-separated keys for recursive masking (e.g. `api_key,secret`) |

### Core

| Env Var | Default | Description |
|---------|---------|-------------|
| `DB_PATH` | `data/observability.db` | SQLite database path |

See `docker-compose.yml` for Docker configuration.

## Tech Stack

- **Proxy**: Python + aiohttp (async, streaming-capable)
- **Core**: Python + FastAPI + SQLite/PostgreSQL
- **UI**: React + TypeScript + Vite + TailwindCSS
- **Deploy**: Docker Compose
