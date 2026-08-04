# Delta: gateway-observability-contract

本 delta 依据 `docs/llm-observability-phase3-rework-bug-fix-requirements.md` 的 P0-1/P0-2/P0-4/P0-7/P0-8/P1-4 冻结修正后的契约语义。

## MODIFIED Requirements

### Requirement: GATEWAY span attribute namespaces

All GATEWAY spans SHALL carry the generic attributes `gateway.name`, `gateway.version`, `gateway.request_id`, `gateway.protocol`, `gateway.route`, `gateway.trace_origin`, `gateway.upstream_trace_present`, and `gateway.span_role`. Router spans SHALL additionally carry `gateway.requested_model`, `gateway.resolved_model`, `gateway.provider`, `gateway.channel_id`, `gateway.channel_type`, `gateway.route_reason`, `gateway.policy_name`, `gateway.retry_count`, `gateway.fallback_count`, `gateway.attempt_count`, `gateway.cache_status`, `gateway.queue_duration_ms`, `gateway.auth_duration_ms`, `gateway.route_duration_ms`, `gateway.total_duration_ms`, `gateway.ttft_ms`, `gateway.final_http_status_code`, `gateway.final_error_type`, and `gateway.final_error_category`. Attempt spans SHALL additionally carry `gateway.attempt_index`, `gateway.provider`, `gateway.channel_id`, `gateway.channel_type`, `gateway.resolved_model`, `gateway.upstream_request_id`, `gateway.upstream_http_status_code`, `gateway.upstream_duration_ms`, `gateway.upstream_connect_duration_ms`, `gateway.upstream_ttft_ms`, `gateway.timeout_ms`, `gateway.retryable`, `gateway.error_type`, `gateway.error_category`, `gateway.error_message`, and `gateway.finish_reason`. Usage attributes SHALL use `usage.input_tokens`, `usage.output_tokens`, `usage.total_tokens`, `usage.cached_input_tokens`, `usage.reasoning_tokens`, `usage.cache_creation_tokens`, `usage.cache_read_tokens`, and `usage.source`. Cost attributes SHALL use `cost.input`, `cost.output`, `cost.total`, `cost.currency`, and `cost.source`.

`gateway.trace_origin` SHALL be frozen to exactly three values, derived from the explicit parent-resolution origin (never inferred indirectly from "whether a parent object exists"):

- `sdk` — the parent came from the in-process SDK context; `gateway.upstream_trace_present` SHALL be `true`.
- `remote` — the parent came from an upstream W3C `traceparent` header; `gateway.upstream_trace_present` SHALL be `true`.
- `gateway` — no SDK context and no upstream traceparent; the Router is a local root; `gateway.upstream_trace_present` SHALL be `false`.

`gateway.trace_origin` and `gateway.upstream_trace_present` SHALL always be consistent with the actual parent IDs on the span: `sdk`/`remote` implies a non-null parent reference, `gateway` implies the Router is the trace root.

Router spans SHALL also carry the complete association field set — `user_id`, `session_id`, `message_id`, `app_name`, and the business-scenario value — written to the Span top-level fields using the EXISTING Span Record naming (`business_scene`; the incompatible spelling `business_scenario` SHALL NOT appear as a second field). Association precedence SHALL be: explicit gateway request value > remote association header/baggage > none. Association values SHALL be sanitized before recording. Attempt spans SHALL NOT duplicate sensitive association fields.

#### Scenario: Router attributes present

- **WHEN** a Router GATEWAY span ends after a successful routed request
- **THEN** the span has `gateway.span_role = "router"` and populated `gateway.resolved_model`, `gateway.channel_id`, `gateway.attempt_count`, and `gateway.total_duration_ms`
- **AND** usage attributes are populated from the Router aggregate

#### Scenario: Attempt attributes present

- **WHEN** an Attempt GATEWAY span ends after an upstream response
- **THEN** the span has `gateway.span_role = "provider_attempt"` and populated `gateway.attempt_index`, `gateway.upstream_http_status_code`, and `gateway.upstream_duration_ms`
- **AND** usage and cost attributes are populated for that single attempt

#### Scenario: SDK context sets trace origin sdk

- **WHEN** the Router's parent is resolved from the in-process SDK context
- **THEN** the Router records `gateway.trace_origin = sdk` and `gateway.upstream_trace_present = true`
- **AND** the Router's parent span ID equals the SDK LLM span ID

#### Scenario: Remote traceparent sets trace origin remote

- **WHEN** the Router's parent is resolved from an upstream `traceparent` header
- **THEN** the Router records `gateway.trace_origin = remote` and `gateway.upstream_trace_present = true`
- **AND** the Router's trace ID and parent span ID match the traceparent values

#### Scenario: Local root sets trace origin gateway

- **WHEN** neither SDK context nor an upstream traceparent exists
- **THEN** the Router records `gateway.trace_origin = gateway` and `gateway.upstream_trace_present = false`
- **AND** the Router has no parent span ID

#### Scenario: Trace metadata consistent with parent IDs

- **WHEN** any Router span is emitted
- **THEN** `gateway.trace_origin = sdk` or `remote` implies a non-null parent reference
- **AND** `gateway.trace_origin = gateway` implies the Router is the trace root

#### Scenario: Router records all association fields

- **WHEN** a gateway request carries `user_id`, `session_id`, `message_id`, `app_name`, and a business-scenario value
- **THEN** the Router span records all five fields under names matching the existing Span Record convention
- **AND** only one business-scenario field name appears
- **AND** explicit gateway values override remote header/baggage values
- **AND** association values are sanitized

### Requirement: Gateway events

The system SHALL record gateway lifecycle as events with fixed names: `gateway.auth.started`, `gateway.auth.completed`, `gateway.auth.failed`, `gateway.route.started`, `gateway.route.selected`, `gateway.route.failed`, `gateway.model.remapped`, `gateway.cache.hit`, `gateway.cache.miss`, `gateway.cache.bypass`, `gateway.rate_limit.checked`, `gateway.rate_limit.rejected`, `gateway.queue.entered`, `gateway.queue.exited`, `gateway.attempt.started`, `gateway.attempt.failed`, `gateway.attempt.completed`, `gateway.retry.scheduled`, `gateway.fallback.selected`, `gateway.stream.started`, `gateway.stream.first_token`, `gateway.stream.completed`, `gateway.stream.cancelled`, `gateway.response.completed`, and `gateway.response.failed`. Event attributes SHALL be limited to `reason`, `attempt_index`, `channel_id`, `from_channel_id`, `to_channel_id`, `provider`, `resolved_model`, `delay_ms`, `error_category`, and `http_status_code`. Events SHALL NOT store the original payload by default.

Every channel identifier appearing in any event (`channel_id`, `from_channel_id`, `to_channel_id`) SHALL be the output of `PrivacyGuard.hash_channel_id()` applied to the internal raw ID; the raw channel ID SHALL NOT appear in any span attribute, event, log, or metric. The `gateway.fallback.selected` event SHALL always carry both `from_channel_id` and `to_channel_id` (hashed) plus `reason`; recording only a to-channel is forbidden. The hash SHALL be stable for the same raw channel ID and SHALL differ for different raw channel IDs.

#### Scenario: Retry decision recorded as event

- **WHEN** a failed attempt triggers a retry
- **THEN** the Router span records a `gateway.retry.scheduled` event with `attempt_index`, `delay_ms`, and `reason`
- **AND** a new Attempt span is created

#### Scenario: Fallback recorded with from/to

- **WHEN** a channel times out and routing switches to another channel
- **THEN** the Router span records a `gateway.fallback.selected` event with hashed `from_channel_id`, hashed `to_channel_id`, and `reason`
- **AND** exactly one fallback event exists for the transition
- **AND** the from-channel differs from the to-channel

#### Scenario: First-token stream event

- **WHEN** the first meaningful content chunk arrives during streaming
- **THEN** a `gateway.stream.first_token` event is recorded exactly once

#### Scenario: Channel ID hashed in route and attempt events

- **WHEN** `gateway.route.selected` or `gateway.attempt.started` events are recorded with a channel
- **THEN** the event `channel_id` value equals `PrivacyGuard.hash_channel_id()` of the raw ID
- **AND** the raw channel ID appears in no span attribute, event, log, or metric

#### Scenario: Channel ID hash stability

- **WHEN** the same raw channel ID is hashed twice, and two different raw channel IDs are hashed
- **THEN** the same raw ID yields the same hash
- **AND** different raw IDs yield different hashes

### Requirement: Streaming lifecycle

Router and Attempt spans SHALL remain open until one of the terminal states: normal full consumption, upstream `[DONE]`, client disconnect, client cancellation, upstream timeout, upstream connection error, generator `close`, or async generator `aclose`. The spans SHALL NOT end, and no successful `AttemptResult` SHALL be aggregated to the Router, at response-header receipt, upstream connection establishment, first-token arrival, or the return of a `StreamingResponse` object. Time-to-first-token SHALL be recorded once as `gateway.ttft_ms` on the Router, `gateway.upstream_ttft_ms` on the Attempt, and a `gateway.stream.first_token` event. On client cancel, the system SHALL record `gateway.stream.cancelled` with `gateway.error_category = client_cancelled`, end both Router and Attempt spans, and clean up all ContextVars, span/attempt registries, streaming wrapper references, background tasks, and HTTP session handles.

Streaming finalization SHALL construct the terminal `AttemptResult` exactly once and aggregate it to the Router exactly once, such that Router and Attempt terminal states are always consistent:

- Streaming success: Attempt status OK, Router status OK, `AttemptResult.success = true`.
- Streaming error: Attempt status ERROR with `gateway.error_category` in (`stream_interrupted`, `timeout`, `connect_error`); Router status ERROR with `gateway.final_error_category` equal to the Attempt's error category.
- Client cancel: Attempt status reflects the frozen cancel semantics with `gateway.error_category = client_cancelled`; Router status ERROR (or the single frozen cancel mapping) with `gateway.final_error_category = client_cancelled`. Router and Attempt SHALL NOT disagree (one OK, one ERROR).

TTFT SHALL be measured from the real upstream request start time (not wrapper creation time) to the first meaningful model content. SSE keepalives, empty strings, metadata-only chunks, usage-only chunks, and the `[DONE]` marker SHALL NOT trigger TTFT. When the terminal chunk carries Usage, that Usage SHALL be recorded on the Attempt and aggregated to the Router; on cancel or failure, partial Usage already returned by the upstream SHALL be recorded per provider capability. Stream close SHALL be idempotent.

#### Scenario: Streaming spans cover full consumption

- **WHEN** a client fully consumes a streaming response
- **THEN** Router and Attempt durations span from request start through the final chunk
- **AND** a `gateway.stream.completed` event is recorded
- **AND** both spans end with status OK
- **AND** the Attempt result is aggregated to the Router exactly once

#### Scenario: Streaming client cancel

- **WHEN** a client disconnects mid-stream
- **THEN** a `gateway.stream.cancelled` event is recorded
- **AND** the Attempt span ends with `gateway.error_category = client_cancelled`
- **AND** the Router span ends with `gateway.final_error_category = client_cancelled`
- **AND** Router and Attempt terminal statuses are consistent (never one OK and one ERROR)
- **AND** no stale Router/Attempt registry entry, ContextVar, or streaming wrapper reference remains

#### Scenario: Streaming error consistent on Router and Attempt

- **WHEN** a stream fails mid-consumption with an interruption or timeout
- **THEN** the Attempt ends with status ERROR and the corresponding `gateway.error_category`
- **AND** the Router ends with status ERROR and `gateway.final_error_category` equal to the Attempt's category

#### Scenario: Span not ended early on StreamingResponse return

- **WHEN** a handler returns a `StreamingResponse` object for a non-consumed stream
- **THEN** neither the Router nor Attempt span has ended at the moment of return
- **AND** no successful `AttemptResult` has been aggregated to the Router

#### Scenario: TTFT ignores non-content chunks

- **WHEN** the stream delivers SSE keepalives, empty chunks, metadata-only or usage-only chunks before real content
- **THEN** TTFT is not recorded for any of them
- **AND** TTFT is measured from the real upstream request start to the first meaningful model content
- **AND** the `[DONE]` marker never triggers TTFT

#### Scenario: Streaming usage aggregated at terminal chunk

- **WHEN** the terminal chunk of a stream carries Usage
- **THEN** the Usage is recorded on the Attempt and included in the Router aggregate
- **AND** partial upstream Usage returned before a cancel or failure is recorded per provider capability

### Requirement: Usage and cost ownership across Attempt, Router, and LLM

Each Attempt SHALL record that single request's Usage and Cost, including Usage returned by a Provider on a failed attempt. The Router SHALL record the aggregate of all Attempt Usages and Costs (including failed attempts), the number of successful and failed attempts, and the final channel. The SDK LLM span SHALL record the logical response Usage seen by the caller; it is NOT required to equal the Router aggregate. The runtime SHALL NOT use a process-local ContextVar to write the Router aggregate back into the SDK LLM span, because SDK and gateway commonly run in different processes. Core/UI SHALL derive from the trace tree: Logical Usage = LLM Usage; Actual Gateway Usage = Router Usage; Retry Waste = Router Usage − final successful Attempt Usage. If the Router aggregate must be returned to the client, it SHALL use an explicit protocol (e.g., response headers such as `x-llm-obs-input-tokens`, `x-llm-obs-output-tokens`, `x-llm-obs-total-cost`) whose signature, size limits, trust boundary, proxy compatibility, and streaming support are designed separately.

#### Scenario: Router usage is the sum of all attempts

- **WHEN** an attempt fails with a billable upstream Usage and a later attempt succeeds
- **THEN** the Router aggregate Usage and Cost include both the failed attempt's and the successful attempt's values

#### Scenario: Failed attempt records usage

- **WHEN** a Provider returns Usage inside an error response
- **THEN** the corresponding Attempt span records that Usage and Cost
- **AND** the Usage is included in the Router aggregate

#### Scenario: LLM usage remains logical response usage

- **WHEN** an SDK LLM call completes through a gateway with retries
- **THEN** the SDK LLM span records the logical response Usage seen by the caller
- **AND** no ContextVar-based write-back of the Router aggregate into the LLM span occurs
- **AND** Retry Waste is derivable as Router aggregate minus the final successful Attempt usage

#### Scenario: Cross-process trace requires no shared ContextVar

- **WHEN** the SDK LLM span and the gateway runtime run in different processes
- **THEN** usage ownership and aggregation still hold via the trace tree without any shared in-process context

## ADDED Requirements

### Requirement: Valid trace identity for gateway roots

When neither an SDK context nor an upstream `traceparent` exists, the Router parent resolver SHALL generate a valid W3C TraceID: exactly 32 lowercase hexadecimal characters, never all zeros, and distinct across consecutive requests. The resolver SHALL return an explicit `ResolvedGatewayParent` (`trace_id`, `parent_span_id`, `origin`, `upstream_trace_present`) with `origin` in (`sdk_context`, `remote_traceparent`, `gateway_root`); a null or all-zero TraceID SHALL never be reported. Every Attempt SHALL inherit its Router's TraceID.

#### Scenario: No-SDK router generates a valid trace ID

- **WHEN** a gateway request arrives with no SDK context and no traceparent
- **THEN** the Router's TraceID is 32 hexadecimal characters, not all zeros
- **AND** consecutive such requests produce distinct TraceIDs
- **AND** each Attempt inherits its Router's TraceID

#### Scenario: Router never reports a null or all-zero trace ID

- **WHEN** any Router span is created under any origin
- **THEN** its TraceID is neither null nor all zeros
