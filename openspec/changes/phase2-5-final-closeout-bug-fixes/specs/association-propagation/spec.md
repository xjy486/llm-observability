## ADDED Requirements

### Requirement: Unified baggage encode/decode contract
The system SHALL provide a single `association_propagation` module with `encode_baggage_value`, `decode_baggage_value`, `build_association_baggage`, `parse_association_baggage`, and `merge_remote_association` functions. SDK Distributed, OpenAI propagation, and Proxy SHALL reuse these semantics. Values containing comma, equals, space, Unicode, control, or percent characters SHALL be W3C percent-encoded on build and percent-decoded on parse.

#### Scenario: Special characters round-trip
- **WHEN** association `user="alice,bob=1 x"` is built into baggage and parsed back
- **THEN** the parsed `user` equals `alice,bob=1 x` exactly

#### Scenario: Unicode round-trip
- **WHEN** association `business_scenario="客户服务"` is built and parsed
- **THEN** the parsed value equals `客户服务`

#### Scenario: Percent character round-trip
- **WHEN** association `message_id="50%off"` is built and parsed
- **THEN** the parsed value equals `50%off`

#### Scenario: Compat header overrides baggage
- **WHEN** a carrier has both `baggage: user=baggage-user` and `X-User-Id: compat-user`
- **THEN** `merge_remote_association` returns `user=compat-user`

### Requirement: Baggage carrier security
The carrier SHALL never contain Prompt, Response, Tool Input/Output, API Key, Authorization, Cookie, or full request body. Association values SHALL apply fail-closed sanitization (masking failure returns `<redacted>`), a maximum length limit, and control-character handling.

#### Scenario: Carrier excludes secrets
- **WHEN** `inject_carrier` is called within a trace that has payload and api_key configured
- **THEN** the returned carrier contains no `payload`, `api_key`, `authorization`, or `cookie` keys

#### Scenario: Masking failure is fail-closed
- **WHEN** an association value triggers an exception during sanitization
- **THEN** the value is replaced with `<redacted>` rather than the raw input
