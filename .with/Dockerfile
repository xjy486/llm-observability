# ============================================================
# Multi-stage Dockerfile for LLM Observability Platform
# Builds: Core (FastAPI) + Proxy (Python) + UI (React/Vite)
# ============================================================

# ─── Stage 1: UI Build ───────────────────────────────────────
FROM node:18-slim AS ui-builder

WORKDIR /app/ui

COPY ui/package.json ui/package-lock.json* ./
RUN npm ci

COPY ui/ .
RUN npm run build

# ─── Stage 2: Python Dependencies ────────────────────────────
FROM python:3.11-slim AS python-deps

RUN pip install --no-cache-dir \
    fastapi>=0.104.0 \
    uvicorn>=0.24.0 \
    pydantic>=2.5.0 \
    aiohttp>=3.9.0

# ─── Stage 3: Production ─────────────────────────────────────
FROM python:3.11-slim AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Create non-root user
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid appuser --shell /bin/bash --create-home appuser

# Copy Python dependencies from deps stage
COPY --from=python-deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=python-deps /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Copy application source
COPY core/ ./core/
COPY proxy/ ./proxy/
COPY common/ ./common/

# Copy built UI from builder stage
COPY --from=ui-builder /app/ui/dist ./ui/dist

# Copy startup script
COPY .with/start.sh ./start.sh
RUN chmod +x ./start.sh

# Create data directory for SQLite
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

EXPOSE 8000

ENV CORE_PORT=8001 \
    PROXY_PORT=8082 \
    UI_PORT=3000 \
    DB_PATH=/data/observability.db

CMD ["./start.sh"]
