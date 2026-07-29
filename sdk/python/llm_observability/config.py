"""SDK configuration."""
from dataclasses import dataclass
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
        capture_retriever_content: Whether to capture retriever document bodies.
    """
    app_name: str = "unknown"
    endpoint: str = "http://localhost:8001"
    api_key: Optional[str] = None
    payload_strategy: str = "masked"
    sample_rate: float = 1.0
    auto_instrument_openai: bool = True
    capture_retriever_content: bool = False
