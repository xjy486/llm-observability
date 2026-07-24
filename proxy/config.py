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
    listen_port: int = 8082

    # Upstream LLM Gateway (One-API, LiteLLM, etc.)
    upstream_url: str = os.getenv("UPSTREAM_URL", "http://localhost:3000")
    upstream_timeout: float = float(os.getenv("UPSTREAM_TIMEOUT", "300"))

    # Observability Core endpoint
    observability_endpoint: str = os.getenv("OBSERVABILITY_ENDPOINT", "http://localhost:8001")
    observability_timeout: float = float(os.getenv("OBSERVABILITY_TIMEOUT", "10"))

    # Payload capture strategy: off / metadata_only / masked / full
    payload_strategy: str = os.getenv("PAYLOAD_STRATEGY", "masked")

    # Gateway name for span identification (configurable, not hardcoded)
    gateway_name: str = os.getenv("GATEWAY_NAME", "llm-proxy")

    # Sampling rate (0.0 - 1.0); errors always captured
    sample_rate: float = float(os.getenv("SAMPLE_RATE", "1.0"))
    error_always_capture: bool = True

    # Sensitive headers to strip/mask
    # P1-5: Internal observability headers are stripped before forwarding to upstream
    sensitive_headers: list = field(default_factory=lambda: [
        "authorization", "api-key", "cookie", "x-api-key",
        "x-auth-token", "proxy-authorization",
        # P1-5: Internal observability headers — consumed by proxy, must not leak to Provider
        "x-llm-obs-span-role",
        "x-session-id",
        "x-user-id",
        "x-app-name",
        "x-business-scene",
    ])

    # Fields to mask in payload (regex-based content masking)
    mask_patterns: list = field(default_factory=lambda: [
        r"sk-[a-zA-Z0-9]+",  # OpenAI keys
        r"Bearer\s+[a-zA-Z0-9\-._]+",
        r"password['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"token['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
        r"secret['\"]?\s*[:=]\s*['\"]?[^\s'\"]+",
    ])

    # Keys whose VALUES should be entirely redacted (key-based masking)
    # P1-NEW-03: MASK_KEYS env var can override/augment the defaults
    _default_mask_keys: list = field(default_factory=lambda: [
        "authorization",
        "api_key",
        "apikey",
        "api-key",
        "x-api-key",
        "x-auth-token",
        "access_token",
        "refresh_token",
        "private_key",
        "secret_key",
        "password",
        "passwd",
        "credential",
        "cookie",
    ])

    @property
    def mask_keys(self) -> list:
        """P1-NEW-03: Merge defaults with MASK_KEYS env var (comma-separated)."""
        env_keys = os.getenv("MASK_KEYS", "")
        if env_keys:
            extra_keys = [k.strip() for k in env_keys.split(",") if k.strip()]
            # Merge: env keys extend (and can override) defaults
            merged = list(self._default_mask_keys)
            for k in extra_keys:
                if k not in merged:
                    merged.append(k)
            return merged
        return self._default_mask_keys

    # Paths to intercept
    observed_paths: list = field(default_factory=lambda: [
        "/v1/chat/completions",
        "/v1/completions",
    ])

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        return cls(
            listen_host=os.getenv("PROXY_HOST", "0.0.0.0"),
            listen_port=int(os.getenv("PROXY_PORT", "8082")),
            upstream_url=os.getenv("UPSTREAM_URL", "http://localhost:3000"),
            upstream_timeout=float(os.getenv("UPSTREAM_TIMEOUT", "300")),
            observability_endpoint=os.getenv("OBSERVABILITY_ENDPOINT", "http://localhost:8001"),
            payload_strategy=os.getenv("PAYLOAD_STRATEGY", "masked"),
            gateway_name=os.getenv("GATEWAY_NAME", "llm-proxy"),
            sample_rate=float(os.getenv("SAMPLE_RATE", "1.0")),
        )
