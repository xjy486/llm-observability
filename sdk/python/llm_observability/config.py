"""SDK configuration."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    """Global SDK configuration.

    Attributes:
        app_name: Name of the application using the SDK.
        endpoint: Observability Core ingest endpoint URL.
        api_key: Optional API key for authentication.
        payload_strategy: Payload collection strategy — off/metadata_only/masked/full.
        sample_rate: Sampling rate between 0.0 and 1.0.
        auto_instrument_openai: Whether to auto-patch OpenAI SDK on init.
        auto_instrument_langchain: Whether to auto-instrument LangChain on init (Phase 2.5).
        capture_retriever_content: Whether to capture retriever document bodies.
        max_attribute_bytes: Max bytes for a single attribute (default 8 KiB, max 128 KiB).
        max_payload_bytes: Max bytes for payload serialization (default 32 KiB).
        fail_open: If True, telemetry errors never block business (default True).
    """
    app_name: str = "unknown"
    endpoint: str = "http://localhost:8001"
    api_key: Optional[str] = None
    payload_strategy: str = "masked"
    sample_rate: float = 1.0
    auto_instrument_openai: bool = True
    auto_instrument_langchain: bool = False
    capture_retriever_content: bool = False
    max_attribute_bytes: int = 8 * 1024
    max_payload_bytes: int = 32 * 1024
    fail_open: bool = True
