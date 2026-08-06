# Delta: gateway-observability-contract

本 delta 收紧 Streaming 终态一致性（恰好一个 Terminal State，first terminal
claim wins）、新增 `gateway.attempt.selected` 事件、冻结 Hedged/Parallel Winner 语
义（Router 终态由显式 Winner 决定，Usage/Cost 仍聚合全部 Attempt，无 Winner 有
确定性 Fail-safe）。不修改已归档历史。

## MODIFIED Requirements

### Requirement: Gateway events

The system SHALL record gateway lifecycle as events with fixed names: `gateway.auth.started`, `gateway.auth.completed`, `gateway.auth.failed`, `gateway.route.started`, `gateway.route.selected`, `gateway.route.failed`, `gateway.model.remapped`, `gateway.cache.hit`, `gateway.cache.miss`, `gateway.cache.bypass`, `gateway.rate_limit.checked`, `gateway.rate_limit.rejected`, `gateway.queue.entered`, `gateway.queue.exited`, `gateway.attempt.started`, `gateway.attempt.failed`, `gateway.attempt.completed`, `gateway.attempt.selected`, `gateway.retry.scheduled`, `gateway.fallback.selected`, `gateway.stream.started`, `gateway.stream.first_token`, `gateway.stream.completed`, `gateway.stream.cancelled`, `gateway.response.completed`, and `gateway.response.failed`. Event attributes SHALL be limited to `reason`, `attempt_index`, `channel_id`, `from_channel_id`, `to_channel_id`, `provider`, `resolved_model`, `delay_ms`, `error_category`, and `http_status_code`. Events SHALL NOT store the original payload by default.

Every channel identifier appearing in any event (`channel_id`, `from_channel_id`, `to_channel_id`) SHALL be the output of `PrivacyGuard.hash_channel_id()` applied to the internal raw ID; the raw channel ID SHALL NOT appear in any span attribute, event, log, or metric. The `gateway.fallback.selected` event SHALL always carry both `from_channel_id` and `to_channel_id` (hashed) plus `reason`; recording only a to-channel is forbidden. The hash SHALL be stable for the same raw channel ID and SHALL differ for different raw channel IDs.

Terminal lifecycle events SHALL be mutually exclusive within a group: the `attempt` group (`gateway.attempt.completed` / `gateway.attempt.failed`), the `response` group (`gateway.response.completed` / `gateway.response.failed`), and the `stream` group (`gateway.stream.completed` / `gateway.stream.cancelled`) SHALL each record at most one event per bound span. Once one event in a group is recorded, every other event in the same group SHALL be rejected. The `gateway.attempt.selected` event SHALL be recorded with `attempt_index`, hashed `channel_id`, `provider`, and `reason`; it MAY be recorded again only when the Winner is explicitly re-selected, and each re-selection SHALL record a new `gateway.attempt.selected` event.

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

#### Scenario: Terminal events mutually exclusive within a group

- **WHEN** a `gateway.attempt.completed` event has been recorded on an Attempt and a `gateway.attempt.failed` event is then requested (or vice versa)
- **THEN** the second event is rejected
- **AND** the Attempt span carries exactly one terminal event from the `attempt` group

#### Scenario: Stream terminal events mutually exclusive

- **WHEN** a `gateway.stream.completed` event has been recorded and a `gateway.stream.cancelled` event is then requested (or vice versa)
- **THEN** the second event is rejected
- **AND** the bound span carries exactly one terminal event from the `stream` group

#### Scenario: Winner selection recorded as event

- **WHEN** the Router selects a Winner attempt
- **THEN** a `gateway.attempt.selected` event is recorded with `attempt_index`, hashed `channel_id`, `provider`, and `reason`
- **AND** the raw channel ID appears in no event attribute

### Requirement: Streaming lifecycle

Router and Attempt spans SHALL remain open until one of the terminal states: normal full consumption, upstream `[DONE]`, client disconnect, client cancellation, upstream timeout, upstream connection error, generator `close`, or async generator `aclose`. The spans SHALL NOT end, and no successful `AttemptResult` SHALL be aggregated to the Router, at response-header receipt, upstream connection establishment, first-token arrival, or the return of a `StreamingResponse` object. Time-to-first-token SHALL be recorded once as `gateway.ttft_ms` on the Router, `gateway.upstream_ttft_ms` on the Attempt, and a `gateway.stream.first_token` event. On client cancel, the system SHALL record `gateway.stream.cancelled` with `gateway.error_category = client_cancelled`, end both Router and Attempt spans, and clean up all ContextVars, span/attempt registries, streaming wrapper references, background tasks, and HTTP session handles.

A stream SHALL reach exactly one Terminal State. The Terminal State SHALL be claimed atomically: the first finalizer path to claim the terminal lock wins, and every other finalizer path (success / error / cancelled racing from concurrent threads, generator `close`, async generator `aclose`, or client disconnect) SHALL be a no-op that does NOT write the Attempt error/status, does NOT aggregate the Router, does NOT record a terminal event, and does NOT close the Attempt or Router. The system SHALL NOT infer terminal priority from timestamps and SHALL NOT implicitly upgrade one terminal state over another; the first claim wins. Consequently a single stream SHALL never carry both a `gateway.stream.completed` and a `gateway.stream.cancelled` event, and the Router and Attempt SHALL never disagree (one OK, one ERROR) because of a terminal race.

`gateway.upstream_duration_ms` on a streaming Attempt SHALL reflect the full upstream stream lifecycle (from the real upstream request start through the terminal state), NOT the response-header time. At the terminal state the wrapper SHALL overwrite `gateway.upstream_duration_ms` with `terminal_time - attempt_start_time`. `gateway.upstream_connect_duration_ms` SHALL continue to reflect the connection-establishment time. Non-streaming Attempt duration semantics SHALL be unchanged.

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

#### Scenario: Streaming terminal race claims exactly one terminal state

- **WHEN** two finalizer paths race for the same stream (stream exhaustion racing client close, stream error racing client close, done-marker racing disconnect, or async cancel racing async aclose)
- **THEN** exactly one path claims the terminal state and proceeds
- **AND** the losing path is a no-op that writes no Attempt error/status, records no terminal event, aggregates nothing to the Router, and closes nothing
- **AND** the stream records exactly one terminal `gateway.stream.*` event
- **AND** the Router and Attempt terminal statuses are consistent

#### Scenario: Streaming upstream duration covers full consumption

- **WHEN** a streaming Attempt reaches its terminal state after full consumption
- **THEN** `gateway.upstream_duration_ms` equals `terminal_time - attempt_start_time` (the full upstream stream lifecycle)
- **AND** `gateway.upstream_duration_ms` is NOT the response-header time
- **AND** `gateway.upstream_connect_duration_ms` remains the connection-establishment time

#### Scenario: Streaming cancel duration covers partial consumption

- **WHEN** a streaming Attempt is cancelled after partial consumption
- **THEN** `gateway.upstream_duration_ms` equals `terminal_time - attempt_start_time` (the partial consumption window up to the cancel)
- **AND** non-streaming Attempt duration semantics are unchanged

### Requirement: Usage and cost ownership across Attempt, Router, and LLM

Each Attempt SHALL record that single request's Usage and Cost, including Usage returned by a Provider on a failed attempt. The Router SHALL record the aggregate of all Attempt Usages and Costs (including failed attempts), the number of successful and failed attempts, and the final channel. The Router's final status, final channel, final HTTP status, final error, and finish reason SHALL be determined by an explicit business Winner (the Attempt the business layer adopted and returned to the caller), NOT by the last-completing Attempt. Usage and Cost aggregation SHALL include every Attempt regardless of which Attempt is the Winner. The SDK LLM span SHALL record the logical response Usage seen by the caller; it is NOT required to equal the Router aggregate. The runtime SHALL NOT use a process-local ContextVar to write the Router aggregate back into the SDK LLM span, because SDK and gateway commonly run in different processes. Core/UI SHALL derive from the trace tree: Logical Usage = LLM Usage; Actual Gateway Usage = Router Usage; Retry Waste = Router Usage − Winner Attempt Usage (the final successful Attempt the business adopted). If the Router aggregate must be returned to the client, it SHALL use an explicit protocol (e.g., response headers such as `x-llm-obs-input-tokens`, `x-llm-obs-output-tokens`, `x-llm-obs-total-cost`) whose signature, size limits, trust boundary, proxy compatibility, and streaming support are designed separately.

Both streaming and non-streaming Attempts SHALL compute Cost from their own captured Usage via the same `CostCalculator` and `resolved_model`; the Router cost aggregate SHALL include every Attempt's Cost (streaming and non-streaming, including failed and force-closed attempts, including losing hedged attempts). The Winner's Cost SHALL be identifiable (so Retry/Hedge Waste = Router aggregate − Winner Cost is derivable), but the Router aggregate SHALL NOT drop losing attempts' Cost.

#### Scenario: Retry cost preserved in Router and LLM aggregates

- **WHEN** an attempt fails with a billable upstream Usage and a later attempt succeeds
- **THEN** the Router aggregate Usage and Cost include both the failed attempt's and the successful attempt's values
- **AND** the SDK LLM span records the logical response Usage seen by the caller (NOT required to equal the Router aggregate)
- **AND** Retry Waste is derivable as the Router aggregate minus the Winner (final successful) Attempt Usage

#### Scenario: Failed attempt records usage

- **WHEN** a Provider returns Usage inside an error response
- **THEN** the corresponding Attempt span records that Usage and Cost
- **AND** the Usage is included in the Router aggregate

#### Scenario: LLM usage remains logical response usage

- **WHEN** an SDK LLM call completes through a gateway with retries
- **THEN** the SDK LLM span records the logical response Usage seen by the caller
- **AND** no ContextVar-based write-back of the Router aggregate into the LLM span occurs
- **AND** Retry Waste is derivable as Router aggregate minus the Winner Attempt usage

#### Scenario: Cross-process trace requires no shared ContextVar

- **WHEN** the SDK LLM span and the gateway runtime run in different processes
- **THEN** usage ownership and aggregation still hold via the trace tree without any shared in-process context

#### Scenario: Streaming and non-streaming attempts carry cost uniformly

- **WHEN** a streaming attempt and a non-streaming attempt resolve to the same priced model
- **THEN** both Attempt spans carry `cost.*` computed from their respective Usage
- **AND** the Router cost aggregate includes both

#### Scenario: Hedged winner defines Router final state

- **WHEN** two parallel (hedged) attempts run, attempt 1 succeeds and its result is returned to the caller, and attempt 2 later times out as the loser
- **THEN** the Router final status is OK (determined by the Winner, attempt 1)
- **AND** the Router final channel and final HTTP status are attempt 1's
- **AND** the Router final error is NOT the loser's timeout
- **AND** the Router Usage and Cost aggregates include both attempt 1 and attempt 2
- **AND** Hedge Waste is derivable as the Router aggregate minus the Winner Cost

#### Scenario: Parallel cost includes losing attempts

- **WHEN** a hedged request has a Winner and one or more losing attempts that consumed billable tokens
- **THEN** the Router cost aggregate includes the losing attempts' Cost
- **AND** the Winner's Cost is identifiable separately from the losing attempts' Cost

#### Scenario: Winner selection is explicit and idempotent

- **WHEN** the Router selects the same Winner attempt index twice
- **THEN** the second selection is idempotent (no duplicate `gateway.attempt.selected` event for the same index)
- **AND** attempting to select an unknown or not-yet-activated attempt is rejected

### Requirement: Retry, fallback, cache, and rate-limit semantics

Each retry SHALL produce a new Attempt span and never mutate a prior attempt's data. A fallback SHALL record `gateway.fallback.selected` with explicit from/to channels and reason; recording only `retry_count += 1` without a channel switch is forbidden. On a cache hit the Router SHALL exist with `gateway.cache_status = hit` and `gateway.attempt_count = 0`, and no Provider Attempt SHALL be created. On a rate-limit rejection the Router SHALL end with status ERROR, `gateway.error_category = rate_limit`, and `gateway.attempt_count = 0`; no fake Attempt SHALL be created when no upstream request was made.

The Router SHALL distinguish the business Winner from the set of all attempts. For a serial retry, the Winner is the last attempt the business layer accepted. For a hedged request, the Winner is the first attempt the business layer adopted and returned to the caller. For a fallback, the Winner is the post-fallback attempt that succeeded. When all attempts fail, the Winner is the attempt whose failure the business layer chose to surface. The Router SHALL provide an explicit `select_winner(attempt_index, reason)` API; the Winner SHALL only be selectable among already-activated attempts that already have an `AttemptResult`. When no Winner is explicitly selected: if exactly one Attempt exists, the Router SHALL auto-select it as the Winner (reason `auto_single_attempt`); if multiple Attempts exist with no explicit Winner, the Router SHALL end with status ERROR, `gateway.final_error_category = gateway_internal`, `gateway.final_error_type = MissingWinnerSelection`, and a `gateway.response.failed` event — the Router SHALL NOT silently treat the last-completing attempt as the Winner.

#### Scenario: Cache hit creates no attempt

- **WHEN** a cached request is served
- **THEN** the Router span has `gateway.cache_status = hit` and `gateway.attempt_count = 0`
- **AND** no Attempt span exists under the Router

#### Scenario: Rate-limit rejection

- **WHEN** the gateway rejects a request due to rate limiting before any upstream call
- **THEN** the Router span ends with status ERROR and `gateway.error_category = rate_limit`
- **AND** `gateway.attempt_count = 0`
- **AND** no Attempt span exists

#### Scenario: Serial retry winner is the accepted attempt

- **WHEN** attempt 1 fails and attempt 2 succeeds and the business layer returns attempt 2
- **THEN** the Router selects attempt 2 as the Winner
- **AND** the Router final status is OK with attempt 2's channel and HTTP status
- **AND** the Router Usage and Cost aggregates include both attempts

#### Scenario: Hedged loser timeout does not override the winner

- **WHEN** a hedged request's Winner has already succeeded and a losing attempt later times out
- **THEN** the Router final status remains OK
- **AND** the Router final channel and final error are the Winner's, not the loser's
- **AND** the losing attempt's Usage and Cost are still included in the Router aggregate

#### Scenario: Multiple attempts without explicit winner is deterministic

- **WHEN** a Router finalizes with multiple Attempts and no explicit Winner selection
- **THEN** the Router ends with status ERROR and `gateway.final_error_category = gateway_internal`
- **AND** `gateway.final_error_type = MissingWinnerSelection`
- **AND** a `gateway.response.failed` event is recorded

#### Scenario: Single attempt auto-selected as winner

- **WHEN** a Router has exactly one Attempt with an `AttemptResult` and no explicit Winner selection
- **THEN** the Router auto-selects that Attempt as the Winner with reason `auto_single_attempt`
- **AND** the Router final status, channel, and HTTP status are that Attempt's
