"""Shared fixtures for gateway_observability tests."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import pytest

from llm_observability import Observability


@pytest.fixture
def clean_sdk():
    """Idempotent SDK init/shutdown per test to avoid cross-test leaks."""
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(
        app_name="gateway-test",
        endpoint="http://localhost:99999",
        auto_instrument_openai=False,
        auto_instrument_langchain=False,
    )
    yield Observability._tracer
    Observability.shutdown()


@pytest.fixture
def tracer(clean_sdk):
    return clean_sdk


@pytest.fixture
def reporter_records(clean_sdk):
    """Return a callable snapshotting the reporter queue."""
    def snapshot():
        return list(clean_sdk.reporter._queue)
    return snapshot
