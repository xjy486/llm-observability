"""
FastAPI application for the Observability Core.

Endpoints:
  POST /api/v1/ingest              — Batch ingest telemetry records
  GET  /api/v1/traces              — List traces with filtering
  GET  /api/v1/traces/{trace_id}   — Get trace detail with span tree
  GET  /api/v1/metrics             — Get dashboard metrics
  GET  /api/v1/timeseries          — Get time series data
  GET  /api/v1/models              — List models with stats
  GET  /api/v1/health              — Health check
"""
import os
import time
import logging
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from models import IngestRequest
from storage import Storage

logger = logging.getLogger("core.api")

# Storage singleton
DB_PATH = os.getenv("DB_PATH", "/tmp/observability.db")
storage = Storage(db_path=DB_PATH)

app = FastAPI(
    title="LLM Observability Core",
    description="Independent LLM/Agent Observability Platform API",
    version="0.1.0",
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/v1/ingest")
async def ingest(request: IngestRequest):
    """Ingest batch of telemetry records from proxy/SDK.

    P0-NEW-04: Returns proper HTTP status:
    - All inserts succeed → 200, status="ok"
    - Some fail → 200, status="partial"
    - All fail → 502, status="error"
    """
    inserted = 0
    errors = []
    for i, record in enumerate(request.records):
        try:
            d = record.model_dump()
            storage.insert_span(d)
            inserted += 1
        except Exception as e:
            logger.error("Failed to insert span %d: %s", i, e)
            errors.append(str(e))

    total = len(request.records)
    failed = total - inserted

    if total > 0 and inserted == 0:
        # All failed
        raise HTTPException(
            status_code=502,
            detail={
                "status": "error",
                "inserted": 0,
                "failed": total,
                "total": total,
                "errors": errors[:10],  # cap error list
            },
        )

    if failed > 0:
        # Partial failure
        return JSONResponse(
            status_code=200,
            content={
                "status": "partial",
                "inserted": inserted,
                "failed": failed,
                "total": total,
                "errors": errors[:10],
            },
        )

    return {"status": "ok", "inserted": inserted, "total": total}


@app.get("/api/v1/traces")
async def list_traces(
    time_start: Optional[float] = Query(None, description="Unix timestamp start"),
    time_end: Optional[float] = Query(None, description="Unix timestamp end"),
    durationMinutes: Optional[int] = Query(None, description="Look back N minutes"),
    status: Optional[str] = Query(None, pattern="^(OK|ERROR|UNSET)$"),
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    app_name: Optional[str] = None,
    business_scene: Optional[str] = None,
    min_duration_ms: Optional[float] = None,
    max_duration_ms: Optional[float] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("start_time", pattern="^(start_time|duration_ms|end_time)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """List traces with filtering and pagination."""
    # Handle durationMinutes
    if durationMinutes is not None and not time_start:
        time_end = time.time()
        time_start = time_end - (durationMinutes * 60)

    # P0-06/P0-07: db.get_trace_summaries returns {"traces": [...], "total": int}
    result = storage.get_trace_summaries(
        time_start=time_start,
        time_end=time_end,
        status=status,
        model=model,
        session_id=session_id,
        user_id=user_id,
        trace_id=trace_id,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        app_name=app_name,
        business_scene=business_scene,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    return {
        "traces": result["traces"],
        "total": result["total"],
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/v1/traces/{trace_id}")
async def get_trace(trace_id: str):
    """Get full trace detail with span tree."""
    result = storage.get_trace_detail(trace_id)
    if not result:
        raise HTTPException(status_code=404, detail="Trace not found")
    return result


@app.get("/api/v1/metrics")
async def get_metrics(
    durationMinutes: Optional[int] = Query(60, description="Look back N minutes"),
    time_start: Optional[float] = None,
    time_end: Optional[float] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
):
    """Get dashboard metrics summary."""
    if time_start is None or time_end is None:
        time_end = time.time()
        time_start = time_end - (durationMinutes * 60)

    return storage.get_metrics(
        time_start=time_start,
        time_end=time_end,
        model=model,
        session_id=session_id,
        user_id=user_id,
    )


@app.get("/api/v1/timeseries")
async def get_timeseries(
    durationMinutes: int = Query(60, ge=1, le=10080),
    intervalSeconds: int = Query(60, ge=10, le=3600),
    model: Optional[str] = None,
):
    """Get time series data for charts."""
    time_end = time.time()
    time_start = time_end - (durationMinutes * 60)

    return storage.get_time_series(
        time_start=time_start,
        time_end=time_end,
        interval_seconds=intervalSeconds,
        model=model,
    )


@app.get("/api/v1/models")
async def list_models():
    """List all models with request/error counts."""
    return {"models": storage.get_models_list()}


@app.get("/api/v1/health")
async def health():
    """Health check."""
    return {
        "status": "healthy",
        "span_count": storage.get_span_count(),
        "db_path": DB_PATH,
    }


@app.get("/api/v1/sessions/{session_id}/traces")
async def get_session_traces(session_id: str):
    """Get all traces for a session (for session view)."""
    result = storage.get_trace_summaries(
        session_id=session_id,
        limit=200,
        sort_by="start_time",
        sort_order="asc",
    )
    return {"session_id": session_id, "traces": result["traces"], "total": result["total"]}
