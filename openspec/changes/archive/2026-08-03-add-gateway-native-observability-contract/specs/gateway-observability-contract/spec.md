# Gateway Observability Contract Specification

## Purpose

Defines the semantic contract for gateway-native observability: the Router/Attempt span hierarchy over the existing SpanKind set, the fixed `gateway.*` / `usage.*` / `cost.*` attribute namespaces, the unified event names, the error taxonomy, Usage/Cost ownership across Attempt/Router/LLM, streaming lifecycle, sampling inheritance, privacy rules, and fail-open behavior. This contract is decoupled from any specific gateway implementation.

## ADDED Requirements

### Requirement: Router and Attempt GATEWAY span hierarchy over existing SpanKinds

The system SHALL represent gateway-native observability using only the existing SpanKind set (`AGENT`, `TASK`, `TOOL`, `LLM`, `GATEWAY`) — never new kinds such as `ROUTER` or `PROVIDER`. The GATEWAY role SHALL be distinguished via the `gateway.span_role` attribute with value `router` or `provider_attempt`. When an SDK LLM span exists, the Router GATEWAY SHALL have `parent_span_id = SDK LLM span_id`; each Attempt GATEWAY SHALL have `parent_span_id = Router span_id`. All spans SHALL share the same TraceID. Every real upstream Provider request SHALL correspond to exactly one unique Attempt span.

#### Scenario: SDK-originated gateway trace

- **WHEN** an SDK LLM call routes through a gateway with two upstream attempts
- **THEN** the trace contains exactly one Router GATEWAY span whose parent is the SDK LLM span
- **AND** exactly two Attempt GATEWAY spans whose parent is the Router span
- **AND** all spans share the same TraceID
- **AND** no span uses a SpanKind other than the existing five

#### Scenario: No shared Attempt span across retries

- **WHEN** an upstream request fails and is retried
- **THEN** each real upstream request produces its own new Attempt span
- **AND** no Attempt span is reused or overwritten by a subsequent attempt

#### Scenario: Attempt never bypasses Router

- **WHEN** a gateway handles a request that has a Router span
- **THEN** no Attempt GATEWAY span is a direct child of the SDK LLM span, bypassing the Router

### Requirement: GATEWAY span attribute namespaces

All GATEWAY spans SHALL carry the generic attributes `gateway.name`, `gateway.version`, `gateway.request_id`, `gateway.protocol`, `gateway.route`, `gateway.trace_origin`, `gateway.upstream_trace_present`, and `gateway.span_role`. Router spans SHALL additionally carry `gateway.requested_model`, `gateway.resolved_model`, `gateway.provider`, `gateway.channel_id`, `gateway.channel_type`, `gateway.route_reason`, `gateway.policy_name`, `gateway.retry_count`, `gateway.fallback_count`, `gateway.attempt_count`, `gateway.cache_status`, `gateway.queue_duration_ms`, `gateway.auth_duration_ms`, `gateway.route_duration_ms`, `gateway.total_duration_ms`, `gateway.ttft_ms`, `gateway.final_http_status_code`, `gateway.final_error_type`, and `gateway.final_error_category`. Attempt spans SHALL additionally carry `gateway.attempt_index`, `gateway.provider`, `gateway.channel_id`, `gateway.channel_type`, `gateway.resolved_model`, `gateway.upstream_request_id`, `gateway.upstream_http_status_code`, `gateway.upstream_duration_ms`, `gateway.upstream_connect_duration_ms`, `gateway.upstream_ttft_ms`, `gateway.timeout_ms`, `gateway.retryable`, `gateway.error_type`, `gateway.error_category`, `gateway.error_message`, and `gateway.finish_reason`. Usage attributes SHALL use `usage.input_tokens`, `usage.output_tokens`, `usage.total_tokens`, `usage.cached_input_tokens`, `usage.reasoning_tokens`, `usage.cache_creation_tokens`, `usage.cache_read_tokens`, and `usage.source`. Cost attributes SHALL use `cost.input`, `cost.output`, `cost.total`, `cost.currency`, and `cost.source`.

#### Scenario: Router attributes present

- **WHEN** a Router GATEWAY span ends after a successful routed request
- **THEN** the span has `gateway.span_role = "router"` and populated `gateway.resolved_model`, `gateway.channel_id`, `gateway.attempt_count`, and `gateway.total_duration_ms`
- **AND** usage attributes are populated from the Router aggregate

#### Scenario: Attempt attributes present

- **WHEN** an Attempt GATEWAY span ends after an upstream response
- **THEN** the span has `gateway.span_role = "provider_attempt"` and populated `gateway.attempt_index`, `gateway.upstream_http_status_code`, and `gateway.upstream_duration_ms`
- **AND** usage and cost attributes are populated for that single attempt

### Requirement: Gateway events

The system SHALL record gateway lifecycle as events with fixed names: `gateway.auth.started`, `gateway.auth.completed`, `gateway.auth.failed`, `gateway.route.started`, `gateway.route.selected`, `gateway.route.failed`, `gateway.model.remapped`, `gateway.cache.hit`, `gateway.cache.miss`, `gateway.cache.bypass`, `gateway.rate_limit.checked`, `gateway.rate_limit.rejected`, `gateway.queue.entered`, `gateway.queue.exited`, `gateway.attempt.started`, `gateway.attempt.failed`, `gateway.attempt.completed`, `gateway.retry.scheduled`, `gateway.fallback.selected`, `gateway.stream.started`, `gateway.stream.first_token`, `gateway.stream.completed`, `gateway.stream.cancelled`, `gateway.response.completed`, and `gateway.response.failed`. Event attributes SHALL be limited to `reason`, `attempt_index`, `channel_id`, `provider`, `resolved_model`, `delay_ms`, `error_category`, and `http_status_code`. Events SHALL NOT store the original payload by default.

#### Scenario: Retry decision recorded as event

- **WHEN** a failed attempt triggers a retry
- **THEN** the Router span records a `gateway.retry.scheduled` event with `attempt_index`, `delay_ms`, and `reason`
- **AND** a new Attempt span is created

#### Scenario: Fallback recorded with from/to

- **WHEN** a channel times out and routing switches to another channel
- **THEN** the Router span records a `gateway.fallback.selected` event with `channel_id` and `reason`
- **AND** exactly one fallback event exists for the transition
- **AND** the from-channel differs from the to-channel

#### Scenario: First-token stream event

- **WHEN** the first meaningful content chunk arrives during streaming
- **THEN** a `gateway.stream.first_token` event is recorded exactly once

### Requirement: Retry, fallback, cache, and rate-limit semantics

Each retry SHALL produce a new Attempt span and never mutate a prior attempt's data. A fallback SHALL record `gateway.fallback.selected` with explicit from/to channels and reason; recording only `retry_count += 1` without a channel switch is forbidden. On a cache hit the Router SHALL exist with `gateway.cache_status = hit` and `gateway.attempt_count = 0`, and no Provider Attempt SHALL be created. On a rate-limit rejection the Router SHALL end with status ERROR, `gateway.error_category = rate_limit`, and `gateway.attempt_count = 0`; no fake Attempt SHALL be created when no upstream request was made.

#### Scenario: Cache hit creates no attempt

- **WHEN** a cached request is served
- **THEN** the Router span has `gateway.cache_status = hit` and `gateway.attempt_count = 0`
- **AND** no Attempt span exists under the Router

#### Scenario: Rate-limit rejection

- **WHEN** the gateway rejects a request due to rate limiting before any upstream call
- **THEN** the Router span ends with status ERROR and `gateway.error_category = rate_limit`
- **AND** `gateway.attempt_count = 0`
- **AND** no Attempt span exists

### Requirement: Streaming lifecycle

Router and Attempt spans SHALL remain open until one of the terminal states: normal full consumption, upstream `[DONE]`, client disconnect, client cancellation, upstream timeout, upstream connection error, generator `close`, or async generator `aclose`. The spans SHALL NOT end at response-header receipt, upstream connection establishment, first-token arrival, or the return of a `StreamingResponse` object. Time-to-first-token SHALL be recorded once as `gateway.ttft_ms` on the Router, `gateway.upstream_ttft_ms` on the Attempt, and a `gateway.stream.first_token` event. On client cancel, the system SHALL record `gateway.stream.cancelled` with `gateway.error_category = client_cancelled`, end both Router and Attempt spans, and clean up all ContextVars, span/attempt registries, streaming wrapper references, background tasks, and HTTP session handles.

#### Scenario: Streaming spans cover full consumption

- **WHEN** a client fully consumes a streaming response
- **THEN** Router and Attempt durations span from request start through the final chunk
- **AND** a `gateway.stream.completed` event is recorded
- **AND** both spans end with status OK

#### Scenario: Streaming client cancel

- **WHEN** a client disconnects mid-stream
- **THEN** a `gateway.stream.cancelled` event is recorded
- **AND** the Attempt span ends with `gateway.error_category = client_cancelled`
- **AND** the Router span also ends
- **AND** no stale Router/Attempt registry entry, ContextVar, or streaming wrapper reference remains

#### Scenario: Span not ended early on StreamingResponse return

- **WHEN** a handler returns a `StreamingResponse` object for a non-consumed stream
- **THEN** neither the Router nor Attempt span has ended at the moment of return

### Requirement: Usage and cost ownership across Attempt, Router, and LLM

Each Attempt SHALL record that single request's Usage and Cost, including Usage returned by a Provider on a failed attempt. The Router SHALL record the aggregate of all Attempt Usages and Costs (including failed attempts), the number of successful and failed attempts, and the final channel. The SDK LLM span SHALL record the logical call's final Usage; when a Router exists, the LLM Usage SHALL equal the Router aggregate, and SHALL NOT be just the final successful attempt's Usage (which would drop the real cost of retries).

#### Scenario: Retry cost preserved in Router and LLM aggregates

- **WHEN** an attempt fails with a billable upstream Usage and a later attempt succeeds
- **THEN** the Router aggregate Cost includes both the failed attempt's and the successful attempt's cost
- **AND** the SDK LLM span Usage equals the Router aggregate
- **AND** the LLM Usage is not limited to the successful attempt

#### Scenario: Failed attempt records usage

- **WHEN** a Provider returns Usage inside an error response
- **THEN** the corresponding Attempt span records that Usage and Cost
- **AND** the Usage is included in the Router aggregate

### Requirement: No-SDK gateway trace

When a gateway request arrives with no upstream SDK trace, the gateway SHALL produce a trace with the Router GATEWAY as the Root Span and its Attempt spans as children. The system SHALL NOT fabricate an LLM or AGENT span. The Router SHALL carry `gateway.upstream_trace_present = false` and `gateway.trace_origin = gateway`.

#### Scenario: Direct gateway request

- **WHEN** a client requests the gateway directly with no `traceparent` from an SDK
- **THEN** a Router GATEWAY span is created as the Root Span with `gateway.upstream_trace_present = false` and `gateway.trace_origin = gateway`
- **AND** the Attempt span's parent is the Router span
- **AND** no LLM or AGENT span exists in the trace

### Requirement: Error classification and safe error messages

The system SHALL classify gateway/upstream failures into a fixed taxonomy: `authentication`, `authorization`, `rate_limit`, `quota`, `timeout`, `connect_error`, `dns_error`, `tls_error`, `provider_4xx`, `provider_5xx`, `invalid_request`, `invalid_response`, `stream_interrupted`, `client_cancelled`, `gateway_internal`, `unknown`. Each failure SHALL be recorded as `gateway.error_type`, `gateway.error_category`, `gateway.error_message`, and `gateway.retryable`. Error messages SHALL be safe strings, length-limited, sanitized, and fail-closed. Telemetry SHALL NOT record Authorization headers, API Keys, Cookies, Provider secrets, full URL query strings, full response bodies, or full stack traces.

#### Scenario: HTTP 500 classified as retryable provider_5xx

- **WHEN** an upstream returns HTTP 500
- **THEN** the Attempt span records `gateway.error_category = provider_5xx` and `gateway.retryable = true`

#### Scenario: Client cancel is distinct from provider error

- **WHEN** a client disconnects mid-stream
- **THEN** the Attempt and Router record `gateway.error_category = client_cancelled`
- **AND** the status is not a normal provider error status

#### Scenario: Secrets absent from error data

- **WHEN** any gateway span, event, or log is emitted during an error
- **THEN** it contains no Authorization header, API Key, Cookie, or full stack trace value

### Requirement: Sampling inheritance and local root creation

When a legitimate upstream `traceparent` exists, its sampling decision SHALL be honored: `trace_flags=01` SHALL sample and `trace_flags=00` SHALL NOT report. The gateway SHALL NOT re-randomize to override the upstream decision. With no upstream trace, the gateway SHALL create a Root Router according to a local `sample_rate`. When sampled out, the system SHALL still run business normally, still propagate `traceparent`, SHALL NOT perform large payload serialization, and SHALL NOT generate Reporter Records.

#### Scenario: Upstream sampled-out is honored

- **WHEN** an incoming `traceparent` has `trace_flags=00`
- **THEN** the gateway performs the business request normally
- **AND** no Reporter Record is generated
- **AND** the upstream sampling decision is not overridden

#### Scenario: No upstream trace creates sampled root

- **WHEN** a request has no upstream trace and the local sample rate samples it in
- **THEN** a Root Router span is created and reported

#### Scenario: Sampled-out still propagates

- **WHEN** the decision is not to sample
- **THEN** the outgoing `traceparent` is still propagated to downstream

### Requirement: Gateway telemetry is fully fail-open

Any observability failure SHALL NOT change gateway business behavior. This covers Router span creation, Attempt span creation, event addition, Usage parsing, Cost calculation, span end, reporter failure, context reset, and streaming finalization. The system SHALL return business success when telemetry fails during a successful request, and SHALL preserve the original business exception when telemetry fails during a failing request.

#### Scenario: Telemetry failure preserves business success

- **WHEN** a request succeeds but an event add or span end raises
- **THEN** the business result is returned unchanged

#### Scenario: Telemetry failure preserves business error

- **WHEN** a request fails with a business exception and span finalization also raises
- **THEN** the original business exception is propagated

#### Scenario: Fail-open on streaming finalization

- **WHEN** stream finalization (wrapper close, registry cleanup, context reset) raises during a client cancel
- **THEN** the client-visible behavior is unchanged and no secondary exception replaces the cancellation outcome
