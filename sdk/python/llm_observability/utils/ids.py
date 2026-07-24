"""ID generation utilities for trace and span identifiers."""
import uuid


def generate_trace_id() -> str:
    """Generate a W3C-compliant trace_id (32 hex chars)."""
    return uuid.uuid4().hex


def generate_span_id() -> str:
    """Generate a W3C-compliant span_id (16 hex chars)."""
    return uuid.uuid4().hex[:16]
