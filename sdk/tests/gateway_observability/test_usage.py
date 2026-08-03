"""UsageNormalizer tests (spec §12.1)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from llm_observability.gateway_observability import UsageNormalizer, add_usage, usage_has_values


def test_openai_compatible_usage_normalized():
    n = UsageNormalizer()
    u = n.normalize({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert u is not None
    assert u.input_tokens == 10
    assert u.output_tokens == 5
    assert u.total_tokens == 15
    assert u.usage_source == "openai"


def test_usage_source_passthrough():
    n = UsageNormalizer()
    u = n.normalize({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, source="one-api")
    assert u.usage_source == "one-api"


def test_cached_and_reasoning_variants():
    n = UsageNormalizer()
    u = n.normalize({
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 40},
        "completion_tokens_details": {"reasoning_tokens": 10},
    })
    assert u is not None
    assert u.cached_input_tokens == 40
    assert u.reasoning_tokens == 10


def test_anthropic_style_cache_fields():
    n = UsageNormalizer(source="anthropic")
    u = n.normalize({
        "input_tokens": 200,
        "output_tokens": 100,
        "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 30,
    })
    assert u is not None
    assert u.cache_creation_tokens == 50
    assert u.cache_read_tokens == 30


def test_object_style_usage():
    class Usage:
        prompt_tokens = 7
        completion_tokens = 3
        total_tokens = 10
    n = UsageNormalizer()
    u = n.normalize(Usage())
    assert u is not None
    assert u.input_tokens == 7
    assert u.output_tokens == 3
    assert u.total_tokens == 10


def test_parse_failure_fail_open():
    n = UsageNormalizer()
    # Non-dict, non-object → no exception, returns None
    assert n.normalize("garbage") is None
    assert n.normalize(12345) is None
    assert n.normalize(None) is None
    # Poison object whose attribute access raises → fail-open
    class Poison:
        @property
        def prompt_tokens(self):
            raise RuntimeError("boom")
    assert n.normalize(Poison()) is None


def test_empty_usage_returns_none():
    n = UsageNormalizer()
    assert n.normalize({}) is None


def test_add_usage_none_safe():
    u1 = UsageNormalizer().normalize({"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})
    u2 = UsageNormalizer().normalize({"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
    agg = add_usage(u1, u2)
    assert agg.input_tokens == 7
    assert agg.output_tokens == 4
    assert agg.total_tokens == 11
    # Adding with None keeps the other side
    agg2 = add_usage(u1, None)
    assert agg2.input_tokens == 5
    assert agg2.total_tokens == 8
    assert usage_has_values(agg2)


def test_add_usage_preserves_failed_attempt():
    """Failed attempt usage is included in the aggregate."""
    failed = UsageNormalizer().normalize({"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100})
    success = UsageNormalizer().normalize({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
    agg = add_usage(success, failed)
    assert agg.input_tokens == 200
    assert agg.total_tokens == 250
