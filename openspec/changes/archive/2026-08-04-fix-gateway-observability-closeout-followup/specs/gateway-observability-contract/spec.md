# Delta: gateway-observability-contract

本 delta 依据归档 closeout 之后的 P1-1/P1-2/Blocker 1 修正 Streaming 与 Usage/Cost 契约语义，不修改已归档历史。

## MODIFIED Requirements

### Requirement: Streaming lifecycle

Router and Attempt spans SHALL remain open until one of the terminal states: normal full consumption, upstream `[DONE]`, client disconnect, client cancellation, upstream timeout, upstream connection error, generator `close`, or async generator `aclose`. The spans SHALL NOT end, and no successful `AttemptResult` SHALL be aggregated to the Router, at response-header receipt, upstream connection establishment, first-token arrival, or the return of a `StreamingResponse` object. Time-to-first-token SHALL be recorded once as `gateway.ttft_ms` on the Router, `gateway.upstream_ttft_ms` on the Attempt, and a `gateway.stream.first_token` event. On client cancel, the system SHALL record `gateway.stream.cancelled` with `gateway.error_category = client_cancelled`, end both Router and Attempt spans, and clean up all ContextVars, span/attempt registries, streaming wrapper references, background tasks, and HTTP session handles.

Streaming finalization SHALL construct the terminal `AttemptResult` exactly once and aggregate it to the Router exactly once, such that Router and Attempt terminal states are always consistent:

- Streaming success: Attempt status OK, Router status OK, `AttemptResult.success = true`.
- Streaming error: Attempt status ERROR with `gateway.error_category` in (`stream_interrupted`, `timeout`, `connect_error`); Router status ERROR with `gateway.final_error_category` equal to the Attempt's error category. An unclassifiable mid-stream interruption SHALL be recorded as `stream_interrupted` (never `unknown`) on the streaming path.
- Client cancel: Attempt status reflects the frozen cancel semantics with `gateway.error_category = client_cancelled`; Router status ERROR (or the single frozen cancel mapping) with `gateway.final_error_category = client_cancelled`. Router and Attempt SHALL NOT disagree (one OK, one ERROR).

TTFT SHALL be measured from the real upstream request start time (not wrapper creation time) to the first meaningful model content. SSE keepalives, empty strings, metadata-only chunks, usage-only chunks, and the `[DONE]` marker SHALL NOT trigger TTFT. When the terminal chunk carries Usage, that Usage SHALL be recorded on the Attempt and aggregated to the Router; the corresponding Cost SHALL be computed from that Usage and the Attempt's `resolved_model` and aggregated to the Router, so streaming and non-streaming attempts carry `cost.*` identically. On cancel or failure, partial Usage already returned by the upstream SHALL be recorded per provider capability and its Cost computed fail-open. Stream close SHALL be idempotent.

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

#### Scenario: Streaming cost computed from terminal usage

- **WHEN** the terminal chunk of a stream carries Usage and the resolved model has a pricing-table entry
- **THEN** the Attempt carries `cost.*` computed from that Usage and the resolved model
- **AND** the Router cost aggregate includes the streaming attempt's cost
- **AND** streaming and non-streaming attempts carry `cost.*` under the same rules

#### Scenario: Unclassified streaming interruption is stream_interrupted

- **WHEN** a stream fails mid-consumption with an exception the global classifier cannot recognize
- **THEN** the Attempt `gateway.error_category` is `stream_interrupted` (not `unknown`)
- **AND** the Router `gateway.final_error_category` is `stream_interrupted`

### Requirement: Usage and cost ownership across Attempt, Router, and LLM

Each Attempt SHALL record that single request's Usage and Cost, including Usage returned by a Provider on a failed attempt. The Router SHALL record the aggregate of all Attempt Usages and Costs (including failed attempts), the number of successful and failed attempts, and the final channel. The SDK LLM span SHALL record the logical response Usage seen by the caller; it is NOT required to equal the Router aggregate. The runtime SHALL NOT use a process-local ContextVar to write the Router aggregate back into the SDK LLM span, because SDK and gateway commonly run in different processes. Core/UI SHALL derive from the trace tree: Logical Usage = LLM Usage; Actual Gateway Usage = Router Usage; Retry Waste = Router Usage − final successful Attempt Usage. If the Router aggregate must be returned to the client, it SHALL use an explicit protocol (e.g., response headers such as `x-llm-obs-input-tokens`, `x-llm-obs-output-tokens`, `x-llm-obs-total-cost`) whose signature, size limits, trust boundary, proxy compatibility, and streaming support are designed separately.

Both streaming and non-streaming Attempts SHALL compute Cost from their own captured Usage via the same `CostCalculator` and `resolved_model`; the Router cost aggregate SHALL include every Attempt's Cost (streaming and non-streaming, including failed and force-closed attempts).

#### Scenario: Retry cost preserved in Router and LLM aggregates

- **WHEN** an attempt fails with a billable upstream Usage and a later attempt succeeds
- **THEN** the Router aggregate Usage and Cost include both the failed attempt's and the successful attempt's values
- **AND** the SDK LLM span records the logical response Usage seen by the caller (NOT required to equal the Router aggregate)
- **AND** Retry Waste is derivable as the Router aggregate minus the final successful Attempt Usage

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

#### Scenario: Streaming and non-streaming attempts carry cost uniformly

- **WHEN** a streaming attempt and a non-streaming attempt resolve to the same priced model
- **THEN** both Attempt spans carry `cost.*` computed from their respective Usage
- **AND** the Router cost aggregate includes both
