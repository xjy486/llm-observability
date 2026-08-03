# Gateway Observability Runtime Specification

## Purpose

Defines the runtime behavior of the gateway-native observability implementation: RouterSpan and AttemptSpan lifecycle and registries, the GatewayEventRecorder, UsageNormalizer and CostCalculator, PrivacyGuard, streaming wrapper, context handling, and propagation. This spec describes implementation-level runtime guarantees that the contract spec relies on.

## ADDED Requirements

### Requirement: RouterSpan and AttemptSpan lifecycle and registries

The runtime SHALL provide `RouterSpan` and `AttemptSpan` runtime objects whose spans remain open from request start until a terminal state. The runtime SHALL maintain per-request Router and Attempt registries and clean them on success, error, client cancel, generator close, async generator aclose, and span-end failure. No ContextVar, Registry entry, streaming wrapper reference, or HTTP session handle SHALL leak after any terminal path.

#### Scenario: Normal completion cleans registries

- **WHEN** a routed request completes normally
- **THEN** the Router and Attempt registry entries, ContextVars, and streaming wrapper references are all cleared

#### Scenario: Registry cleanup after client cancel

- **WHEN** a client cancels mid-stream
- **THEN** the Router and Attempt registries are empty afterward
- **AND** no ContextVar for the active gateway context remains

#### Scenario: Span end failure still cleans up

- **WHEN** `span.end()` raises during finalization
- **THEN** registries and ContextVars are still cleaned and the failure does not leak into business behavior

### Requirement: GatewayEventRecorder

The runtime SHALL provide a `GatewayEventRecorder` that records the fixed gateway events with the limited attribute set from the contract. A recorder failure SHALL be fail-open and SHALL NOT alter the span state or business outcome.

#### Scenario: Event add failure is fail-open

- **WHEN** adding a gateway event raises
- **THEN** the span and business flow continue unchanged

### Requirement: UsageNormalizer and CostCalculator

The runtime SHALL provide a `UsageNormalizer` that maps provider-specific usage payloads (OpenAI chat completion, Anthropic Messages, and OpenAI-compatible responses) into `NormalizedUsage` (`input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`, `reasoning_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `usage_source`) and a `CostCalculator` that maps normalized usage to `NormalizedCost` (`input_cost`, `output_cost`, `total_cost`, `currency`, `cost_source`). Normalization and cost-calc failures SHALL be fail-open: the span still ends with whatever data was successfully recorded.

#### Scenario: OpenAI-compatible usage normalized

- **WHEN** an OpenAI-compatible response contains `prompt_tokens`, `completion_tokens`, and `total_tokens`
- **THEN** the normalizer yields `input_tokens`, `output_tokens`, and `total_tokens` with a populated `usage_source`

#### Scenario: Usage parse failure is fail-open

- **WHEN** a usage payload cannot be parsed
- **THEN** no exception propagates and the span ends with the successfully recorded attributes

#### Scenario: Cost calculation failure is fail-open

- **WHEN** the cost calculator raises
- **THEN** no exception propagates and the cost attributes remain unset or partially set

### Requirement: PrivacyGuard

The runtime SHALL provide a `PrivacyGuard` that blocks by default the recording of Authorization headers, API Keys, Cookies, Set-Cookie, raw channel secrets, full upstream URLs, full prompts, full responses, tool inputs, tool outputs, and uploaded files. It SHALL allow provider names, hashed channel IDs, model names, HTTP status, Usage, Cost, error categories, and request IDs. Channel IDs SHALL be hashed/HMAC'd from internal raw IDs by default, and the system SHALL NOT persist secret names or tokens.

#### Scenario: Channel ID hashed

- **WHEN** a gateway records a channel ID
- **THEN** the telemetry value is the hash/HMAC of the internal raw ID, not the raw ID

#### Scenario: Secret attributes blocked

- **WHEN** telemetry emission encounters an Authorization header or API Key value
- **THEN** the value is not recorded in any span, event, or log

### Requirement: Gateway context and propagation

The runtime SHALL manage an active gateway context via ContextVars covering the current Router and current Attempt, and SHALL restore the parent context on success, error, GeneratorExit, and async generator `aclose`. The runtime SHALL accept a valid W3C `traceparent` from an upstream SDK trace and use its TraceID and sampling decision; with no upstream trace it SHALL create a Root Router. Context reset failures SHALL be fail-open.

#### Scenario: Context restored on error

- **WHEN** a router/attempt context exits with an error
- **THEN** the parent context is restored and no stale gateway ContextVar remains

#### Scenario: Context reset failure is fail-open

- **WHEN** context reset raises during exit
- **THEN** the business exception or result is preserved

### Requirement: Streaming wrapper lifecycle

The runtime SHALL provide a streaming wrapper that keeps Router and Attempt spans open across a stream and ends them only at a terminal state (full consumption, `[DONE]`, client disconnect/cancel, upstream timeout, upstream connection error, generator `close`, async generator `aclose`). It SHALL record TTFT exactly once and the corresponding `gateway.stream.*` events. On any terminal path, including failures during stream finalization, it SHALL clean up ContextVars, registries, wrapper references, background tasks, and HTTP session handles.

#### Scenario: Streaming wrapper tracks full consumption

- **WHEN** a stream is consumed to completion
- **THEN** the wrapper ends both spans with status OK after the final chunk and records `gateway.stream.completed`

#### Scenario: Streaming wrapper handles early close

- **WHEN** a consumer closes the generator or async generator before completion
- **THEN** the wrapper ends both spans, records `gateway.stream.cancelled`, and cleans all registry and ContextVar state

#### Scenario: Stream finalization failure is fail-open

- **WHEN** wrapper finalization raises
- **THEN** the consumer-visible stream behavior is unchanged
