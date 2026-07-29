"""Phase 2.5 final closeout — Unified Baggage contract (P0-4)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "proxy"))
import pytest
from llm_observability import Observability
from llm_observability.association_propagation import (
    encode_baggage_value, decode_baggage_value, build_association_baggage,
    parse_association_baggage, merge_remote_association, extract_compat_headers,
)


def _clean_init():
    if Observability._initialized:
        Observability.shutdown()
    Observability.init(app_name="t", endpoint="http://localhost:99999",
                       auto_instrument_openai=False)
    return Observability._tracer


def test_encode_decode_roundtrip_special_chars():
    raw = "alice,bob=1 x"
    encoded = encode_baggage_value(raw)
    assert "," not in encoded and "=" not in encoded
    assert decode_baggage_value(encoded) == raw


def test_unicode_roundtrip():
    raw = "客户服务"
    encoded = encode_baggage_value(raw)
    assert decode_baggage_value(encoded) == raw


def test_percent_character_roundtrip():
    raw = "50%off"
    encoded = encode_baggage_value(raw)
    assert decode_baggage_value(encoded) == raw


def test_control_chars_safely_encoded():
    raw = "a\tb\nc"
    encoded = encode_baggage_value(raw)
    decoded = decode_baggage_value(encoded)
    assert decoded == raw


def test_build_parse_roundtrip():
    props = {"user": "alice,bob", "session_id": "s 1", "message_id": "m%1",
             "business_scenario": "cs", "app_name": "app"}
    baggage = build_association_baggage(props)
    parsed = parse_association_baggage(baggage)
    for k in props:
        assert parsed[k] == props[k]


def test_merge_remote_compat_overrides_baggage():
    baggage = build_association_baggage({"user": "baggage-user"})
    compat = {"user": "compat-user"}
    merged = merge_remote_association(baggage, compat)
    assert merged["user"] == "compat-user"


def test_extract_compat_headers_case_insensitive():
    headers = {"X-USER-ID": "u1", "x-session-id": "s1"}
    compat = extract_compat_headers(headers)
    assert compat["user"] == "u1"
    assert compat["session_id"] == "s1"


def test_distributed_baggage_special_chars_roundtrip():
    _clean_init()
    tracer = Observability._tracer
    with Observability.association_context(user="alice,bob=1 x"):
        with tracer.trace(name="root"):
            carrier = Observability.inject_carrier()
            extracted = Observability.extract_carrier(carrier)
            assert extracted is not None
            assert extracted.association["user"] == "alice,bob=1 x"
    Observability.shutdown()


def test_carrier_does_not_include_payload_or_api_key():
    _clean_init()
    tracer = Observability._tracer
    with tracer.trace(name="root"):
        carrier = Observability.inject_carrier()
        for forbidden in ("payload", "api_key", "authorization", "cookie"):
            assert forbidden not in {k.lower() for k in carrier}
    Observability.shutdown()


def test_proxy_baggage_percent_decode():
    """Proxy trace_context decodes percent-encoded baggage (shared contract)."""
    import trace_context as tc
    baggage = build_association_baggage({"user": "alice,bob", "message_id": "m%1"})
    headers = {"baggage": baggage, "X-User-Id": "compat-user"}
    meta = tc.extract_metadata_headers(headers)
    # compat overrides baggage
    assert meta["user_id"] == "compat-user"
    # message_id decoded from baggage
    assert meta["message_id"] == "m%1"


def test_proxy_compat_header_overrides_baggage():
    import trace_context as tc
    baggage = build_association_baggage({"session_id": "baggage-sess"})
    headers = {"baggage": baggage, "X-Session-Id": "compat-sess"}
    meta = tc.extract_metadata_headers(headers)
    assert meta["session_id"] == "compat-sess"


def test_baggage_masking_failure_is_fail_closed():
    # A value that triggers no exception still sanitizes; verify <redacted> on failure
    # by passing an object whose __str__ raises
    class Bad:
        def __str__(self):
            raise RuntimeError("nope")
    from llm_observability.association_propagation import _sanitize_value
    # _sanitize_value catches and returns <redacted>
    result = _sanitize_value(Bad())
    assert result == "<redacted>"
