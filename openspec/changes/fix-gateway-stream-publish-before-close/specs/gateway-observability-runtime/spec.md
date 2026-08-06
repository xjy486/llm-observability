# Delta: gateway-observability-runtime

本 delta 修复归档 router-context-weakref 之后的 1 个冻结阻塞（streaming close-before-publish）+ 1 个文档矛盾（slot 清理语义），不修改已归档历史。

## MODIFIED Requirements

### Requirement: RouterSpan and AttemptSpan lifecycle and registries

The runtime SHALL provide `RouterSpan` and `AttemptSpan` runtime objects whose spans remain open from request start until a terminal state. The runtime SHALL maintain per-request Router and Attempt registries and clean them on success, error, client cancel, generator close, async generator aclose, and span-end failure. No ContextVar, Registry entry, streaming wrapper reference, or HTTP session handle SHALL leak after any terminal path.

The Router SHALL explicitly track its open Attempts (`_open_attempts: dict[str, AttemptSpan]`): an Attempt registers on start and unregisters on close. The `_open_attempts` registry SHALL be protected by an independent re-entrant lock covering registration, unregistration, snapshot, force-close iteration, and clear, so concurrent attempts, hedged requests, or a Router-finalize racing an Attempt-close cannot miss or double an entry. Router `finalize()` SHALL force-close every remaining open Attempt via an idempotent, fail-open `force_close(category="gateway_internal", reason="router_finalized_with_open_attempt")`. Merely deleting registry entries without ending the spans is forbidden. `force_close()` SHALL NOT overwrite an already-recorded business error, SHALL clean the registry and context entries, and SHALL report the final span. After Router finalize, the open-attempt registry SHALL be empty and no attempt SHALL be reported twice.

Once a Router has reached its terminal state (`_closed = True`), it SHALL reject registration of any new Attempt: `register_open_attempt` SHALL be a no-op that returns False when the Router is closed, and `RouterSpan.attempt()` / `AttemptSpan.start()` SHALL treat a closed Router as a fail-open no-op telemetry path — no registry entry, no active-attempt ContextVar, no reportable orphan span — while business continues. The Router SHALL set `_closed = True` atomically with the open-attempt snapshot+clear under the same lock, so a concurrent register either is captured in the snapshot or observes the closed state and is rejected. `RouterSpan.attempt()` SHALL check `_closed` and allocate the Attempt index under the lifecycle lock, so a Router that finalizes during allocation cannot bump `attempt_count` / `_used_attempt_indices` without a real, activatable Attempt; `gateway.attempt_count` SHALL always equal the number of real Attempt spans.

`AttemptSpan.start()` SHALL make activation atomic with `force_close()` / `close()` via a per-Attempt re-entrant lifecycle lock. The post-activation confirmation — re-check `_closed`, install the active-attempt ContextVar, and record `gateway.attempt.started` — SHALL run as one critical section (`_activate_context_and_started_event`). `force_close()` and `close()` SHALL take the same lifecycle lock for their `_closed`-state transition, so only two orders are possible: `start` completes the ContextVar + event installation (then `force_close` runs and clears the token it can now see), or `force_close` closes first and `start`'s critical section observes `_closed` and installs nothing — no intermediate "closed-check passed but finalize lands before ContextVar install" window. A post-install re-check inside the critical section SHALL clear a just-installed token if finalize lands between `set_attempt` and the event. The Attempt SHALL NOT depend on a later business-thread `close()` to clean up a leaked token. A rejected (no-op) Attempt SHALL drop its `Span` reference so no started-but-unended Span object lingers.

Because Python `ContextVar`s are per-Context/per-thread, a finalize running on another thread CANNOT reset a worker thread's `active_attempt` or `active_router` via the saved token. BOTH slots in `GatewayContextState` SHALL therefore hold **weak references** (the Router and the Attempt), not strong references. `GatewayContext.get()` SHALL dereference BOTH lazily, with slot-scoped clearing: a dead/closed Router (`_closed`) SHALL clear the current thread's WHOLE context (both Router and Attempt slots — the Router's Attempt is moot once the Router is gone); a dead/closed Attempt WHILE the Router is alive SHALL clear ONLY the Attempt slot, preserving the Router slot so Retry / Fallback / the next Attempt / subsequent Router events keep working. The cleared slots SHALL read `None`. `Runtime.active_router()` SHALL never return an ended Router, and `Runtime.active_attempt()` SHALL never return an ended Attempt. This makes cross-thread cleanup unnecessary for either slot — an ended Router (and its `_attempts`, which transitively pin Attempts) stops surfacing and stops pinning the moment any thread reads the context, so a long-lived worker / thread-pool thread reused after a cross-thread `Router.finalize()` observes no stale `active_router` (and no transitive Attempt retention), even when the owner never called `close()`. The public `GatewayContextState.router` / `.active_attempt` fields SHALL remain dereferencing accessors (returning the live `RouterSpan` / `AttemptSpan` or `None`), not the weak-ref holders, preserving the public API contract.

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

#### Scenario: Cross-thread Router finalize hides the closed Router on the owner thread

- **WHEN** a Router fully activates on thread A and is later finalized (`handle.finalize()`) on thread B, and thread A never exits its scope, then A re-reads the gateway context
- **THEN** `Runtime.active_router()` returns `None` (the ended Router is not surfaced), via lazy invalidation of the weak Router slot
- **AND** the weak Router slot no longer pins `Router._attempts`, so the ended Router's Attempts are collectable

#### Scenario: Thread-pool worker reused after Router finalize has no active router

- **WHEN** a worker thread runs a request whose `handle.finalize()` is invoked from another thread (owner never closes), and the same worker is then reused for a second task that reads the gateway context
- **THEN** the reused worker's `active_router` reads `None` (no stale ended Router)
- **AND** `active_attempt` also reads `None`

### Requirement: Streaming wrapper lifecycle

The runtime SHALL provide a streaming wrapper that keeps Router and Attempt spans open across a stream and ends them only at a terminal state (full consumption, `[DONE]`, client disconnect/cancel, upstream timeout, upstream connection error, generator `close`, async generator `aclose`). The runtime SHALL split finalization into `finish_non_streaming_attempt(...)` and `finalize_streaming_attempt(...)`; the streaming path SHALL NOT finish the Attempt or aggregate a success result to the Router at response-header receipt or wrapper creation. At the terminal state the wrapper SHALL construct the final `AttemptResult`, PUBLISH it to the Router (via `try_aggregate_result`, exactly-once, publish-before-claim), THEN close the Attempt, THEN close the Router — in that order — through a single `_publish_and_close(result)` funnel shared by the success / error / cancel paths. Publishing BEFORE closing the Attempt guarantees a racing `Router.finalize()` either still sees the open Attempt (and force-closes it as a no-op re-aggregation) or sees the already-published aggregate, so the reported Router Record always reflects the result. Router and Attempt terminal states SHALL be consistent per the contract. The wrapper SHALL record TTFT exactly once (from the real upstream request start, ignoring keepalives, empty chunks, metadata-only or usage-only chunks, and `[DONE]`) and the corresponding `gateway.stream.*` events. On any terminal path, including failures during stream finalization, it SHALL clean up ContextVars, registries, wrapper references, background tasks, and HTTP session handles. Stream close SHALL be idempotent: repeated `close()`/`aclose()` or cancellation after finalization SHALL NOT re-aggregate or re-report.

At the terminal state the streaming finalizer SHALL compute the Attempt Cost from the captured terminal Usage using the `CostCalculator` and the Attempt's `resolved_model` (fail-open), write it to the Attempt, include it in the `AttemptResult`, and aggregate it into the Router cost aggregate — so streaming attempts carry `cost.*` exactly like non-streaming attempts. For a stream error or cancel, when the classifier returns `unknown`, the streaming finalizer SHALL remap the category to `stream_interrupted`; this remap is local to the streaming funnel and SHALL NOT alter global `classify_error()` behavior.

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
