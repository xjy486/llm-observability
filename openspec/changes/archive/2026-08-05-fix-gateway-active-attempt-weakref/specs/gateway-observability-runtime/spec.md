# Delta: gateway-observability-runtime

本 delta 处理归档 attempt-lifecycle-lock 之后的 2 个残余缺陷（全激活后跨线程 force_close 无法清理 owner ContextVar / 同一 Attempt 多路径重复聚合），不修改已归档历史。

## MODIFIED Requirements

### Requirement: RouterSpan and AttemptSpan lifecycle and registries

The runtime SHALL provide `RouterSpan` and `AttemptSpan` runtime objects whose spans remain open from request start until a terminal state. The runtime SHALL maintain per-request Router and Attempt registries and clean them on success, error, client cancel, generator close, async generator aclose, and span-end failure. No ContextVar, Registry entry, streaming wrapper reference, or HTTP session handle SHALL leak after any terminal path.

The Router SHALL explicitly track its open Attempts (`_open_attempts: dict[str, AttemptSpan]`): an Attempt registers on start and unregisters on close. The `_open_attempts` registry SHALL be protected by an independent re-entrant lock covering registration, unregistration, snapshot, force-close iteration, and clear, so concurrent attempts, hedged requests, or a Router-finalize racing an Attempt-close cannot miss or double an entry. Router `finalize()` SHALL force-close every remaining open Attempt via an idempotent, fail-open `force_close(category="gateway_internal", reason="router_finalized_with_open_attempt")`. Merely deleting registry entries without ending the spans is forbidden. `force_close()` SHALL NOT overwrite an already-recorded business error, SHALL clean the registry and context entries, and SHALL report the final span. After Router finalize, the open-attempt registry SHALL be empty and no attempt SHALL be reported twice.

Once a Router has reached its terminal state (`_closed = True`), it SHALL reject registration of any new Attempt: `register_open_attempt` SHALL be a no-op that returns False when the Router is closed, and `RouterSpan.attempt()` / `AttemptSpan.start()` SHALL treat a closed Router as a fail-open no-op telemetry path — no registry entry, no active-attempt ContextVar, no reportable orphan span — while business continues. The Router SHALL set `_closed = True` atomically with the open-attempt snapshot+clear under the same lock, so a concurrent register either is captured in the snapshot or observes the closed state and is rejected. `RouterSpan.attempt()` SHALL check `_closed` and allocate the Attempt index under the lifecycle lock, so a Router that finalizes during allocation cannot bump `attempt_count` / `_used_attempt_indices` without a real, activatable Attempt; `gateway.attempt_count` SHALL always equal the number of real Attempt spans.

`AttemptSpan.start()` SHALL make activation atomic with `force_close()` / `close()` via a per-Attempt re-entrant lifecycle lock. The post-activation confirmation — re-check `_closed`, install the active-attempt ContextVar, and record `gateway.attempt.started` — SHALL run as one critical section (`_activate_context_and_started_event`). `force_close()` and `close()` SHALL take the same lifecycle lock for their `_closed`-state transition, so only two orders are possible: `start` completes the ContextVar + event installation (then `force_close` runs and clears the token it can now see), or `force_close` closes first and `start`'s critical section observes `_closed` and installs nothing — no intermediate "closed-check passed but finalize lands before ContextVar install" window. A post-install re-check inside the critical section SHALL clear a just-installed token if finalize lands between `set_attempt` and the event. The Attempt SHALL NOT depend on a later business-thread `close()` to clean up a leaked token. A rejected (no-op) Attempt SHALL drop its `Span` reference so no started-but-unended Span object lingers.

Because Python `ContextVar`s are per-Context/per-thread, a finalize running on another thread CANNOT reset a worker thread's `active_attempt` via the saved token. The active-attempt slot in `GatewayContextState` SHALL therefore hold a **weak reference** to the Attempt (not a strong reference). `GatewayContext.get()` SHALL dereference it lazily: when the referent is dead OR closed (`_closed`), the current thread's attempt slot SHALL be cleared (a per-thread `ContextVar.set`, no foreign token) and `active_attempt` SHALL read `None`. `Runtime.active_attempt()` SHALL never return an ended Attempt. This makes cross-thread cleanup unnecessary — an ended Attempt stops surfacing the moment any thread reads the context, so a long-lived worker / thread-pool thread reused after a cross-thread force_close observes no stale `active_attempt`, even when the owner never called `close()`.

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

#### Scenario: Router finalize force-closes open attempts

- **WHEN** a business exception occurs between Attempt start and close, and the Router is then finalized
- **THEN** every open Attempt is force-closed with `gateway_internal` / `router_finalized_with_open_attempt`
- **AND** the open-attempt registry is empty
- **AND** each force-closed Attempt's final span is reported exactly once

#### Scenario: force_close is idempotent and preserves business errors

- **WHEN** `force_close()` is called twice on the same Attempt, or on an Attempt that already recorded a business error
- **THEN** the second call is a no-op
- **AND** the previously recorded business error is not overwritten
- **AND** no duplicate report is emitted after a normally ended Attempt

#### Scenario: Open-attempt registry is concurrency-safe

- **WHEN** Attempts start, close, and force-close concurrently, or a Router finalize races an Attempt close
- **THEN** no registry entry is missed or double-counted
- **AND** the finalize snapshot is stable (an attempt that closed before finalize is not force-closed; one still open is)

#### Scenario: Closed Router rejects new Attempt registration

- **WHEN** an Attempt starts or registers after the Router has finalized (closed)
- **THEN** registration is rejected (no-op) and no orphan span, registry entry, or active-attempt ContextVar is created
- **AND** the business request continues without an exception

#### Scenario: Attempt start racing Router finalize leaves no leak

- **WHEN** a concurrent Attempt start races a Router finalize
- **THEN** either the Attempt is captured in the finalize snapshot and force-closed, or its registration is rejected
- **AND** after finalize the open-attempt registry is empty and no orphan span is reportable

#### Scenario: No ContextVar leak when finalize races register→set_attempt

- **WHEN** a Router finalizes (force-closing a registered-but-not-yet-active Attempt) in the window between `register_open_attempt` returning True and `set_attempt`, and `start()` then resumes
- **THEN** `start()` does NOT set `active_attempt` on the racing thread
- **AND** no `gateway.attempt.started` event is recorded on the ended Attempt span
- **AND** the Attempt's later `close()` leaves no `active_attempt` ContextVar behind on any thread

#### Scenario: Index allocation does not bump attempt_count for a closed Router

- **WHEN** `RouterSpan.attempt()` races a Router finalize
- **THEN** `gateway.attempt_count` is unchanged (no index allocated) unless the Attempt is actually activatable
- **AND** the Router `_used_attempt_indices` is not mutated by a rejected attempt

#### Scenario: Rejected Attempt leaves no started Span object

- **WHEN** an Attempt is rejected at activation because the Router is closed
- **THEN** the Attempt holds no `Span` reference (no started-but-unended Span lingers in memory)
- **AND** it is neither reported nor registered

#### Scenario: No ContextVar leak when finalize lands inside the set_attempt window

- **WHEN** a Router finalize (force_close) lands after the Attempt's closed-check has returned False but before/inside `GatewayContext.set_attempt`, and `start()` then resumes
- **THEN** the lifecycle lock serializes the two: either `start` finishes installing the ContextVar (then `force_close` clears it), or `force_close` closes first (then `start`'s critical section installs nothing)
- **AND** no `gateway.attempt.started` event is recorded on the ended Attempt span
- **AND** no leaked `active_attempt` ContextVar survives on the racing thread even if the business owner never calls `close()`

#### Scenario: Cross-thread force_close after full activation clears the owner context lazily

- **WHEN** an Attempt fully activates on thread A (ContextVar installed, started recorded, lock released) and A never calls `close()`, and a Router finalize on thread B then force-closes the Attempt
- **THEN** thread B's cross-thread token reset cannot reach thread A's ContextVar, but the weak-referenced active-attempt slot is lazily invalidated on read
- **AND** a subsequent read of `GatewayContext.get().active_attempt` on thread A returns `None` (the ended Attempt is not surfaced)
- **AND** `Runtime.active_attempt()` never returns the ended Attempt

#### Scenario: Thread-pool worker reused after force_close has no active attempt

- **WHEN** a worker thread runs an Attempt whose Router is force-finalized from another thread (owner never calls `close()`), and the same worker thread is then reused for a second task that reads the gateway context
- **THEN** the reused worker's `active_attempt` reads `None` (no stale ended Attempt)
- **AND** no business exception propagates from the stale-slot read

### Requirement: UsageNormalizer and CostCalculator

The runtime SHALL provide a `UsageNormalizer` that maps provider-specific usage payloads (OpenAI chat completion, Anthropic Messages, and OpenAI-compatible responses) into `NormalizedUsage` (`input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`, `reasoning_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `usage_source`) and a `CostCalculator` that maps normalized usage to `NormalizedCost` (`input_cost`, `output_cost`, `total_cost`, `currency`, `cost_source`). Normalization and cost-calc failures SHALL be fail-open: the span still ends with whatever data was successfully recorded.

Attempt cost SHALL be calculated with the Attempt's resolved model: `calculate(usage=normalized, model=attempt.resolved_model)`. The pricing table SHALL use explicit per-1M-token units, configured under unambiguous names `input_usd_per_1m_tokens` and `output_usd_per_1m_tokens`. When no price exists for the resolved model, `cost.source` SHALL be `unpriced`. For cache hits, an explicitly caller-provided cost SHALL be preserved; when no cost is provided but usage exists, cost SHALL be computed from the resolved model. Router cost SHALL include the cost of failed and retried attempts. Streaming attempts SHALL compute Cost from their captured terminal Usage by the same `CostCalculator.calculate(usage, model=resolved_model)` path as non-streaming attempts.

`RouterSpan.register_attempt_result` — which mutates `_success_count`/`_fail_count`, `_final_error`, `_final_http_status`, `_final_channel_id`, `_ttft_ms`, `_usage_aggregate`, and `_cost_aggregate` — SHALL be protected by an independent re-entrant aggregate lock so that concurrent (hedged / parallel provider) attempt results aggregate without lost updates or torn read-modify-writes on the usage/cost aggregates. The same lock SHALL guard the cache-hit direct setters (`set_usage_aggregate` / `set_cost_aggregate`) and the aggregate read at Router finalize.

Exactly-once aggregation of a single Attempt across all paths (non-streaming `finalize_attempt`, the streaming terminal finalizer, and `force_close`) SHALL be enforced by a single Attempt-owned funnel `try_aggregate_result(result)`: the `_aggregated_to_router` check-and-set SHALL be atomic under the Attempt's lifecycle lock, and only the first caller SHALL call `Router.register_attempt_result`. A later caller (e.g. `force_close` racing a `finish_attempt` that already aggregated) SHALL be a no-op, so the same Attempt is never counted twice regardless of which path wins.

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
- **AND** the Router cost aggregate is the exact sum of all attempts' cost

#### Scenario: Same attempt aggregated exactly once across racing paths

- **WHEN** `finish_attempt` and `force_close` (or the streaming finalizer and `force_close`) race to aggregate the SAME Attempt
- **THEN** exactly one of them aggregates the result (the other is a no-op via `try_aggregate_result`)
- **AND** the Router's `success_count` + `fail_count` increments by exactly one for that attempt
