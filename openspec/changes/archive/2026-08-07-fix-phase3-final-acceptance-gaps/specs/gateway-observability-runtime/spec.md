# Delta: gateway-observability-runtime

本 delta 收紧 Runtime：Streaming 终态原子状态机（first terminal claim wins）+
Streaming Duration 语义、Recorder Terminal Event 互斥组、`register_attempt_result`
不再覆盖 `final_*` 并新增 `select_winner` + Winner 驱动 Router 终态 + 无 Winner
Fail-safe。不修改已归档历史。

## MODIFIED Requirements

### Requirement: GatewayEventRecorder

The runtime SHALL provide a `GatewayEventRecorder` that records the fixed gateway events with the limited attribute set from the contract. A recorder failure SHALL be fail-open and SHALL NOT alter the span state or business outcome. The recorder SHALL be wired into the actual runtime lifecycle: `gateway.route.selected` on route selection, `gateway.attempt.started` on Attempt start, `gateway.attempt.completed` on Attempt success, `gateway.attempt.failed` on Attempt failure, `gateway.attempt.selected` on Winner selection, and `gateway.response.completed` / `gateway.response.failed` at Router terminal states. Each terminal event SHALL be recorded at most once and SHALL be written before the span ends; event attributes SHALL pass through the PrivacyGuard. Terminal events SHALL be mutually exclusive within a group: the `attempt` group (`gateway.attempt.completed` / `gateway.attempt.failed`), the `response` group (`gateway.response.completed` / `gateway.response.failed`), and the `stream` group (`gateway.stream.completed` / `gateway.stream.cancelled`) SHALL each record at most one event per bound span. Once one event in a group is recorded, every other event in the same group SHALL be rejected (return False). The `stream` events SHALL be part of the terminal set (not just the attempt/response events). An Attempt SHALL never carry both a completed and a failed event; a stream SHALL never carry both a completed and a cancelled event.

#### Scenario: Event add failure is fail-open

- **WHEN** adding a gateway event raises
- **THEN** the span and business flow continue unchanged

#### Scenario: Lifecycle events recorded exactly once

- **WHEN** an Attempt starts, then succeeds or fails
- **THEN** exactly one `gateway.attempt.started` event exists
- **AND** exactly one terminal event (`gateway.attempt.completed` or `gateway.attempt.failed`, never both) exists, written before span end

#### Scenario: Router terminal event exactly once

- **WHEN** a Router reaches a success or failure terminal state
- **THEN** exactly one of `gateway.response.completed` / `gateway.response.failed` is recorded before the Router span ends

#### Scenario: Attempt completed then failed rejected

- **WHEN** a `gateway.attempt.completed` event has been recorded and a `gateway.attempt.failed` event is then requested on the same recorder
- **THEN** the `gateway.attempt.failed` event is rejected (not recorded)
- **AND** the Attempt span carries exactly one terminal event

#### Scenario: Attempt failed then completed rejected

- **WHEN** a `gateway.attempt.failed` event has been recorded and a `gateway.attempt.completed` event is then requested on the same recorder
- **THEN** the `gateway.attempt.completed` event is rejected (not recorded)

#### Scenario: Response completed then failed rejected

- **WHEN** a `gateway.response.completed` event has been recorded and a `gateway.response.failed` event is then requested on the same Router recorder
- **THEN** the `gateway.response.failed` event is rejected

#### Scenario: Stream completed then cancelled rejected

- **WHEN** a `gateway.stream.completed` event has been recorded and a `gateway.stream.cancelled` event is then requested on the same recorder
- **THEN** the `gateway.stream.cancelled` event is rejected
- **AND** the bound span carries exactly one terminal event from the `stream` group

#### Scenario: Winner selection event recorded

- **WHEN** the Router selects a Winner attempt
- **THEN** a `gateway.attempt.selected` event is recorded with `attempt_index`, hashed `channel_id`, `provider`, and `reason`
- **AND** the raw channel ID does not appear in the event

### Requirement: UsageNormalizer and CostCalculator

The runtime SHALL provide a `UsageNormalizer` that maps provider-specific usage payloads (OpenAI chat completion, Anthropic Messages, and OpenAI-compatible responses) into `NormalizedUsage` (`input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`, `reasoning_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `usage_source`) and a `CostCalculator` that maps normalized usage to `NormalizedCost` (`input_cost`, `output_cost`, `total_cost`, `currency`, `cost_source`). Normalization and cost-calc failures SHALL be fail-open: the span still ends with whatever data was successfully recorded.

Attempt cost SHALL be calculated with the Attempt's resolved model: `calculate(usage=normalized, model=attempt.resolved_model)`. The pricing table SHALL use explicit per-1M-token units, configured under unambiguous names `input_usd_per_1m_tokens` and `output_usd_per_1m_tokens`. When no price exists for the resolved model, `cost.source` SHALL be `unpriced`. For cache hits, an explicitly caller-provided cost SHALL be preserved; when no cost is provided but usage exists, cost SHALL be computed from the resolved model. Router cost SHALL include the cost of failed and retried attempts, and SHALL include the cost of losing hedged attempts. Streaming attempts SHALL compute Cost from their captured terminal Usage by the same `CostCalculator.calculate(usage, model=resolved_model)` path as non-streaming attempts.

`RouterSpan.register_attempt_result` — which mutates `_success_count`/`_fail_count`, the `_results_by_index` map, `_ttft_ms`, `_usage_aggregate`, and `_cost_aggregate` — SHALL be protected by an independent re-entrant aggregate lock so that concurrent (hedged / parallel provider) attempt results aggregate without lost updates or torn read-modify-writes on the usage/cost aggregates. `register_attempt_result` SHALL store the `AttemptResult` in `_results_by_index` (keyed by `attempt_index`) for Winner resolution, and SHALL still aggregate Usage and Cost from every attempt; it SHALL NOT overwrite `_final_channel_id`, `_final_http_status`, or `_final_error` per call. The same lock SHALL guard the cache-hit direct setters (`set_usage_aggregate` / `set_cost_aggregate`), the Winner-selection state (`_selected_attempt_index` / `_selected_result` / `_selection_reason`), and the aggregate read at Router finalize.

The Router SHALL provide `select_winner(attempt_index: int, reason: str | None = None) -> bool`. `select_winner` SHALL only accept an already-activated attempt that already has an `AttemptResult` in `_results_by_index`; selecting an unknown or result-less attempt SHALL return False. Selecting the same `attempt_index` twice SHALL be idempotent (return True, no duplicate `gateway.attempt.selected` event). Switching the Winner to a different `attempt_index` SHALL be allowed and SHALL record a new `gateway.attempt.selected` event with `reason` indicating re-selection. On `select_winner`, the Router SHALL set `_selected_attempt_index` / `_selected_result` / `_selection_reason` under the aggregate lock and SHALL derive `_final_channel_id` / `_final_http_status` / `_final_error` / finish reason from the Winner's `AttemptResult`. At `Router.finalize()`, if no Winner has been selected: if exactly one `AttemptResult` exists, the Router SHALL auto-select it with `reason="auto_single_attempt"`; if multiple `AttemptResult`s exist with no Winner, the Router SHALL end with status ERROR, `gateway.final_error_category = gateway_internal`, `gateway.final_error_type = MissingWinnerSelection`, and a `gateway.response.failed` event — it SHALL NOT treat the last-completing attempt as an implicit Winner. The reported Router Record's `final_*` fields SHALL always reflect the resolved Winner (or the deterministic Fail-safe), never the last-completing attempt.

Exactly-once aggregation of a single Attempt across all paths (non-streaming `finalize_attempt`, the streaming terminal finalizer, and `force_close`) SHALL be enforced by a single Attempt-owned funnel `try_aggregate_result(result)`: the `_aggregated_to_router` check, the `Router.register_attempt_result(result)` publish, and the `_aggregated_to_router = True` claim SHALL ALL run atomically under the Attempt's lifecycle lock, with the publish BEFORE the claim. A `Router.finalize()` that observes `_aggregated_to_router=True` is thus guaranteed the result is already in the Router aggregate — the reported Router Record SHALL reflect the fully-published aggregate (no result published after the Router report). Lock ordering SHALL be `Attempt._lifecycle_lock` → `Router._aggregate_lock`, never reversed.

#### Scenario: OpenAI-compatible usage normalized

- **WHEN** an OpenAI-compatible response contains `prompt_tokens`, `completion_tokens`, and `total_tokens`
- **THEN** the normalizer yields `input_tokens`, `output_tokens`, and `total_tokens` with a populated `usage_source`

#### Scenario: Usage parse failure is fail-open

- **WHEN** a usage payload cannot be parsed
- **THEN** no exception propagates and the span ends with the successfully recorded attributes

#### Scenario: Cost calculation failure is fail-open

- **WHEN** the cost calculator raises
- **THEN** no exception propagates and the cost attributes remain unset or partially set

#### Scenario: Cost uses the resolved model

- **WHEN** an Attempt resolves to a model present in the pricing table
- **THEN** the Attempt cost is computed with that resolved model's per-1M-token prices
- **AND** the pricing configuration names are `input_usd_per_1m_tokens` / `output_usd_per_1m_tokens`

#### Scenario: Unknown model is unpriced

- **WHEN** the resolved model has no pricing entry
- **THEN** `cost.source = unpriced`

#### Scenario: Cache explicit cost preserved; retry costs summed

- **WHEN** a cache hit supplies an explicit cost
- **THEN** that cost is used instead of recomputation
- **AND** when retries occur, the Router cost sums all attempts including failed ones

#### Scenario: Parallel attempt results aggregate exactly

- **WHEN** N attempt results are aggregated concurrently (hedged / parallel provider attempts)
- **THEN** `success_count` + `fail_count` equals N (no lost count updates)
- **AND** the Router usage aggregate is the exact sum of all attempts' usage (no overwritten read-modify-writes)
- **AND** the Router cost aggregate is the exact sum of all attempts' cost (including losing attempts)

#### Scenario: register_attempt_result does not overwrite final state

- **WHEN** a successful attempt 1 aggregates and then a losing attempt 2 (timeout) aggregates
- **THEN** `register_attempt_result` does not overwrite `_final_channel_id` / `_final_http_status` / `_final_error` with attempt 2's values
- **AND** both attempts' Usage and Cost are included in the Router aggregate
- **AND** `_results_by_index` contains both attempt 1 and attempt 2 results

#### Scenario: select_winner defines Router final state

- **WHEN** the Router calls `select_winner(attempt_index=1, reason="first_success")` after both attempts have aggregated
- **THEN** the Router `_final_channel_id` / `_final_http_status` / `_final_error` reflect attempt 1's `AttemptResult`
- **AND** a `gateway.attempt.selected` event is recorded with attempt 1's hashed channel and `reason`
- **AND** the Router final status is OK

#### Scenario: select_winner is idempotent

- **WHEN** `select_winner(attempt_index=1)` is called twice
- **THEN** the second call returns True and records no additional `gateway.attempt.selected` event

#### Scenario: select_winner rejects unknown attempt

- **WHEN** `select_winner(attempt_index=99)` is called for an attempt with no `AttemptResult`
- **THEN** the call returns False and no `gateway.attempt.selected` event is recorded

#### Scenario: Multiple attempts without winner is deterministic

- **WHEN** a Router finalizes with multiple `AttemptResult`s and no explicit Winner
- **THEN** the Router ends with status ERROR and `gateway.final_error_category = gateway_internal`
- **AND** `gateway.final_error_type = MissingWinnerSelection`
- **AND** a `gateway.response.failed` event is recorded

#### Scenario: Single attempt auto-selected as winner

- **WHEN** a Router finalizes with exactly one `AttemptResult` and no explicit Winner
- **THEN** the Router auto-selects that attempt with `reason="auto_single_attempt"`
- **AND** the Router final status, channel, and HTTP status are that attempt's

#### Scenario: Same attempt aggregated exactly once across racing paths

- **WHEN** `finish_attempt` and `force_close` (or the streaming finalizer and `force_close`) race to aggregate the SAME Attempt
- **THEN** exactly one of them aggregates the result (the other is a no-op via `try_aggregate_result`)
- **AND** the Router's `success_count` + `fail_count` increments by exactly one for that attempt

#### Scenario: Router finalize waits for a claimed attempt's published result

- **WHEN** `try_aggregate_result` has published the result to the Router but a `Router.finalize()` races before the claim is observable, OR `finalize()` observes `_aggregated_to_router=True`
- **THEN** the result is guaranteed already in the Router aggregate (publish-before-claim), so the reported Router Record includes the result's `usage.*`, `cost.*`, `final_error_category`, and counts
- **AND** no result is published after the Router Report has been emitted

#### Scenario: Reported Router Record matches the final in-memory aggregate

- **WHEN** a racing `finish_attempt`/`force_close` aggregation and a `Router.finalize()` report occur
- **THEN** the Router Record captured by the Reporter has the same status, `final_error_category`, `usage.*`, `cost.*`, and `attempt_count` as the Router's final in-memory state
- **AND** the streaming-finalize-vs-router-finalize race aggregates exactly one (not zero)

### Requirement: Streaming wrapper lifecycle

The runtime SHALL provide a streaming wrapper that keeps Router and Attempt spans open across a stream and ends them only at a terminal state (full consumption, `[DONE]`, client disconnect/cancel, upstream timeout, upstream connection error, generator `close`, async generator `aclose`). The runtime SHALL split finalization into `finish_non_streaming_attempt(...)` and `finalize_streaming_attempt(...)`; the streaming path SHALL NOT finish the Attempt or aggregate a success result to the Router at response-header receipt or wrapper creation. The wrapper SHALL claim a single Terminal State atomically via a terminal lock (`_claim_terminal(state) -> bool`): the first finalizer path to claim wins, and every other racing finalizer path (success / error / cancelled, generator `close`, async generator `aclose`, client disconnect) SHALL be a no-op that writes no Attempt error/status, records no terminal event, aggregates nothing to the Router, and closes no span. The system SHALL NOT infer terminal priority from timestamps and SHALL NOT implicitly upgrade one terminal over another; first terminal claim wins. At the terminal state the wrapper SHALL construct the final `AttemptResult`, PUBLISH it to the Router (via `try_aggregate_result`, exactly-once, publish-before-claim), THEN close the Attempt, THEN close the Router — in that order — through a single `_publish_and_close(result)` funnel shared by the success / error / cancel paths. Publishing BEFORE closing the Attempt guarantees a racing `Router.finalize()` either still sees the open Attempt (and force-closes it as a no-op re-aggregation) or sees the already-published aggregate, so the reported Router Record always reflects the result. Router and Attempt terminal states SHALL be consistent per the contract. The wrapper SHALL record TTFT exactly once (from the real upstream request start, ignoring keepalives, empty chunks, metadata-only or usage-only chunks, and `[DONE]`) and the corresponding `gateway.stream.*` events. On any terminal path, including failures during stream finalization, it SHALL clean up ContextVars, registries, wrapper references, background tasks, and HTTP session handles. Stream close SHALL be idempotent: repeated `close()`/`aclose()` or cancellation after finalization SHALL NOT re-aggregate or re-report.

At the terminal state the streaming finalizer SHALL compute the Attempt Cost from the captured terminal Usage using the `CostCalculator` and the Attempt's `resolved_model` (fail-open), write it to the Attempt, include it in the `AttemptResult`, and aggregate it into the Router cost aggregate — so streaming attempts carry `cost.*` exactly like non-streaming attempts. For a stream error or cancel, when the classifier returns `unknown`, the streaming finalizer SHALL remap the category to `stream_interrupted`; this remap is local to the streaming funnel and SHALL NOT alter global `classify_error()` behavior. At the terminal state the wrapper SHALL overwrite the Attempt's `gateway.upstream_duration_ms` with `terminal_time - attempt_start_time` (the full upstream stream lifecycle, including partial consumption on cancel), captured at the moment the terminal claim is won; `gateway.upstream_connect_duration_ms` SHALL remain the connection-establishment time. Non-streaming Attempt duration semantics SHALL be unchanged.

#### Scenario: Streaming wrapper tracks full consumption

- **WHEN** a stream is consumed to completion
- **THEN** the wrapper ends both spans with status OK after the final chunk and records `gateway.stream.completed`
- **AND** the Attempt result is registered with the Router exactly once

#### Scenario: Streaming wrapper handles early close

- **WHEN** a consumer closes the generator or async generator before completion
- **THEN** the wrapper ends both spans with consistent cancel semantics, records `gateway.stream.cancelled`, and cleans all registry and ContextVar state
- **AND** repeated close/aclose calls finalize only once

#### Scenario: No success aggregation before terminal state

- **WHEN** response headers have been received and the wrapper created, but the stream has not terminated
- **THEN** no successful `AttemptResult` has been aggregated to the Router
- **AND** neither span has ended

#### Scenario: Stream finalization failure is fail-open

- **WHEN** wrapper finalization raises
- **THEN** the consumer-visible stream behavior is unchanged

#### Scenario: Streaming terminal Usage produces Cost

- **WHEN** a terminal stream chunk carries Usage and the resolved model has a pricing-table entry
- **THEN** the Attempt carries `cost.*` computed from that Usage and the resolved model
- **AND** the Router cost aggregate includes the streaming attempt's cost
- **AND** when the resolved model has no pricing entry, `cost.source = unpriced` and no exception propagates

#### Scenario: Unclassified streaming interruption is stream_interrupted

- **WHEN** a stream fails mid-consumption with an exception the global classifier cannot recognize (e.g. a parse or protocol error)
- **THEN** the Attempt `gateway.error_category` is `stream_interrupted` (not `unknown`)
- **AND** the Router `gateway.final_error_category` equals `stream_interrupted`

#### Scenario: Streaming publishes the result before closing the Attempt

- **WHEN** a streaming terminal state is reached and a `Router.finalize()` races the gap between the old close-then-aggregate order
- **THEN** the streaming funnel (`_publish_and_close`) aggregates the `AttemptResult` to the Router BEFORE closing the Attempt (so the Attempt is still registered during publish)
- **AND** the reported Router Record includes the result's status, `final_error_category`, `usage.*`, `cost.*`, and `attempt_count` (no result published after the Router Report)
- **AND** a racing `Router.finalize()` either force-closes the still-open Attempt (no-op re-aggregation) or sees the already-published aggregate

#### Scenario: Stream exhaustion racing close claims one terminal

- **WHEN** the stream-exhaustion success path and a concurrent client `close`/`aclose` cancel path race for the same stream, coordinated by `threading.Event`/`Barrier`/`asyncio.Event` (no `sleep()` timing)
- **THEN** exactly one path claims the terminal state via `_claim_terminal`
- **AND** the losing path is a no-op that writes no Attempt error/status, records no terminal event, and aggregates nothing
- **AND** exactly one `gateway.stream.*` terminal event is recorded

#### Scenario: Stream error racing close claims one terminal

- **WHEN** a stream-error path and a concurrent client-close cancel path race for the same stream
- **THEN** exactly one path claims the terminal state
- **AND** Router and Attempt terminal statuses are consistent (the losing path did not leave a conflicting state)

#### Scenario: Done marker racing disconnect claims one terminal

- **WHEN** an upstream `[DONE]` marker and a concurrent client disconnect race for the same stream
- **THEN** exactly one path claims the terminal state and proceeds to `_publish_and_close`
- **AND** the other path is a no-op

#### Scenario: Async cancel racing aclose claims one terminal

- **WHEN** an async cancel and a concurrent async generator `aclose` race for the same stream
- **THEN** exactly one path claims the terminal state
- **AND** the wrapper finalizes only once (idempotent)

#### Scenario: Stream total duration covers consumption

- **WHEN** a streaming Attempt reaches its terminal state after full consumption
- **THEN** `gateway.upstream_duration_ms` equals `terminal_time - attempt_start_time` (the full upstream stream lifecycle)
- **AND** `gateway.upstream_duration_ms` is NOT the response-header-time value written at wrapper creation
- **AND** `gateway.upstream_connect_duration_ms` is unchanged

#### Scenario: Stream cancel total duration covers partial consumption

- **WHEN** a streaming Attempt is cancelled after partial consumption
- **THEN** `gateway.upstream_duration_ms` equals `terminal_time - attempt_start_time` (the partial consumption window up to the cancel)

#### Scenario: Non-streaming duration semantics unchanged

- **WHEN** a non-streaming Attempt completes via `finish_non_streaming_attempt`
- **THEN** its `gateway.upstream_duration_ms` semantics are unchanged from prior behavior
- **AND** no streaming-only duration overwrite is applied
