"""
Proxy configuration.
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProxyConfig:
    # Network
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080

    # Upstream LLM Gateway (One-API, LiteLLM, etc.)
    upstream_url: str = os.getenv("UPSTREAM_URL", "http://localhost:3000")
    upstream_timeout: float = float(os.getenv("UPSTREAM_TIMEOUT", "300"))

    # Observability Core endpoint
    observability_endpoint: str = os.getenv("OBSERVABILITY_ENDPOINT", "http://localhost:8001")
    observability_timeout: float = float(os.getenv("OBSERVABILITY_TIMEOUT", "10"))

    # Payload capture strategy: off / metadata_only / masked / full
    payload_strategy: str = os.getenv("PAYLOAD_STRATEGY", "masked")

    # Sampling rate (0.0 - 1.0); errors always captured
    sample_rate: float = float(os.getenv("SAMPLE_RATE", "1.0"))
    error_always_capture: bool = True

    # Sensitive headers to strip/mask
    sensitive_headers: list = field(default_factory=lambda: [
        "authorization", "api-key", "cookie", "x-api-key",
        "x-auth-token", "proxy-authorization",
    ])

    # Fields to mask in payload
    mask_patterns: list = field(default_factory=lambda: [
        r"sk-[a-zA-Z0-9]+",  # OpenAI keys
        r"Bearer\s+[a-zA-Z0-9\-._]+",
        r"password['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"token['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"secret['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
    ])

    # Paths to intercept
    observed_paths: list = field(default_factory=lambda: [
        "/v1/chat/completions",
        "/v1/completions",
    ])

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        return cls(
            listen_host=os.getenv("PROXY_HOST", "0.0.0.0"),
            listen_port=int(os.getenv("PROXY_PORT", "8080")),
            upstream_url=os.getenv("UPSTREAM_URL", "http://localhost:3000"),
            upstream_timeout=float(os.getenv("UPSTREAM_TIMEOUT", "300")),
            observability_endpoint=os.getenv("OBSERVABILITY_ENDPOINT", "http://localhost:8001"),
            payload_strategy=os.getenv("PAYLOAD_STRATEGY", "masked"),
            sample_rate=float(os.getenv("SAMPLE_RATE", "1.0")),
        )
