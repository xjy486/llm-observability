#!/usr/bin/env python3
"""Stream-redact CI secrets from logs (rework P0-6).

Reads secret values from the environment and replaces every occurrence on
stdin with ``<redacted>``, writing to stdout. Values are NEVER embedded in
shell expressions (the previous single-quoted ``sed 's/$VAR/.../g'`` never
expanded the variable and leaked real secrets).

Usage:
    python -m pytest ... 2>&1 | python scripts/redact_ci_secrets.py

Environment:
    CI_REDACT_SECRETS  — comma-separated NAMES of env vars whose values must
                         be redacted. Defaults to the gateway/E2E secret set.
"""
import os
import sys

DEFAULT_SECRET_NAMES = (
    "GATEWAY_E2E_API_KEY",
    "GATEWAY_E2E_BASE_URL",
    "GATEWAY_E2E_MODEL",
    "E2E_API_KEY",
    "E2E_BASE_URL",
    "E2E_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

REDACTED = "<redacted>"
MIN_SECRET_LEN = 4  # never redact trivially short values (false positives)


def collect_secrets() -> list[str]:
    names = os.environ.get("CI_REDACT_SECRETS")
    secret_names = [n.strip() for n in names.split(",") if n.strip()] if names else list(DEFAULT_SECRET_NAMES)
    secrets = []
    for name in secret_names:
        value = os.environ.get(name)
        if value and len(value) >= MIN_SECRET_LEN:
            secrets.append(value)
    # Longest-first so overlapping secrets redact cleanly.
    secrets.sort(key=len, reverse=True)
    return secrets


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def main() -> int:
    secrets = collect_secrets()
    for line in sys.stdin:
        sys.stdout.write(redact(line, secrets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
