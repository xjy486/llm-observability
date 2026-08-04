# Delta: gateway-observability-runtime

本 delta 处理归档 closeout-followup 之后的 2 个冻结阻塞状态一致性问题（force_close 对已聚合 Attempt 的误判 / Router 终态后仍可注册新 Attempt），不修改已归档历史。

## MODIFIED Requirements

### Requirement: RouterSpan and AttemptSpan lifecycle and registries

The runtime SHALL provide `RouterSpan` and `AttemptSpan` runtime objects whose spans remain open from request start until a terminal state. The runtime SHALL maintain per-request Router and Attempt registries and clean them on success, error, client cancel, generator close, async generator aclose, and span-end failure. No ContextVar, Registry entry, streaming wrapper reference, or HTTP session handle SHALL leak after any terminal path.

The Router SHALL explicitly track its open Attempts (`_open_attempts: dict[str, AttemptSpan]`): an Attempt registers on start and unregisters on close. The `_open_attempts` registry SHALL be protected by an independent re-entrant lock covering registration, unregistration, snapshot, force-close iteration, and clear, so concurrent attempts, hedged requests, or a Router-finalize racing an Attempt-close cannot miss or double an entry. Router `finalize()` SHALL force-close every remaining open Attempt via an idempotent, fail-open `force_close(category="gateway_internal", reason="router_finalized_with_open_attempt")`. Merely deleting registry entries without ending the spans is forbidden. `force_close()` SHALL NOT overwrite an already-recorded business error, SHALL clean the registry and context entries, and SHALL report the final span. After Router finalize, the open-attempt registry SHALL be empty and no attempt SHALL be reported twice.

Once a Router has reached its terminal state (`_closed = True`), it SHALL reject registration of any new Attempt: `register_open_attempt` SHALL be a no-op that returns False when the Router is closed, and `RouterSpan.attempt()` / `AttemptSpan.start()` SHALL treat a closed Router as a fail-open no-op telemetry path — no registry entry, no active-attempt ContextVar, no reportable orphan span — while business continues. The Router SHALL set `_closed = True` atomically with the open-attempt snapshot+clear under the same lock, so a concurrent register either is captured in the snapshot or observes the closed state and is rejected.

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

### Requirement: Force-closed attempt aggregates failure to the Router

When `Router.finalize()` force-closes an open Attempt, the force-close SHALL first check whether the Attempt already has a final business result aggregated (`_aggregated_to_router = True`). If it does, force-close SHALL only close the span — preserving the already-recorded OK or business-ERROR status — and SHALL NOT write a `gateway_internal` error, SHALL NOT re-aggregate, and SHALL NOT change the Router's outcome. Only an Attempt with no aggregated result SHALL be marked `gateway_internal` ERROR and have its failure `AttemptResult` aggregated into the Router exactly once (idempotent via the attempt's `_aggregated_to_router` guard). Consequently, for a never-aggregated open Attempt the Router's `fail_count` SHALL increment, its `final_error_category` SHALL be `gateway_internal` (unless a business error on a later/other attempt supersedes it), and the Router SHALL end `ERROR` with exactly one `gateway.response.failed` event — never `OK` while a child Attempt is `ERROR`. A force-closed Attempt that already recorded a business error SHALL aggregate that business error (not `gateway_internal`) and SHALL NOT be re-aggregated.

#### Scenario: Force-closed attempt makes the Router ERROR

- **WHEN** a Router finalizes with one open Attempt that was never finalized (no aggregated result) and no prior successful attempt
- **THEN** the Router span ends with status `ERROR`
- **AND** `gateway.final_error_category` is `gateway_internal`

#### Scenario: Force-closed attempt records response_failed exactly once

- **WHEN** a Router finalizes with an open Attempt that was never finalized
- **THEN** exactly one `gateway.response.failed` event is recorded on the Router before it ends
- **AND** no `gateway.response.completed` event is recorded

#### Scenario: Multiple force-closed attempts increment fail_count

- **WHEN** a Router finalizes with N open never-finalized Attempts and no prior successful attempt
- **THEN** the Router `fail_count` is N
- **AND** each open Attempt is reported exactly once

#### Scenario: Force-closed attempt with a business error preserves it

- **WHEN** an open Attempt already recorded a business error (e.g. `timeout`) and is then force-closed at Router finalize
- **THEN** the aggregated `final_error_category` is the business error category (not `gateway_internal`)
- **AND** the Attempt is not re-aggregated if it was already finalized

#### Scenario: Finalized-success-but-open force-close keeps both OK

- **WHEN** an Attempt had `finish_attempt` aggregate a successful result (`_aggregated_to_router = True`, Router success) but was never `close()`d, and the Router then finalizes
- **THEN** the Attempt span ends with status `OK`
- **AND** no `gateway_internal` error is written on the Attempt
- **AND** the Router remains `OK` with `success_count` unchanged
- **AND** no `gateway.response.failed` event is recorded for this attempt

#### Scenario: Finalized-error-but-open force-close keeps the same error

- **WHEN** an Attempt had `finish_attempt` aggregate a business error result but was never `close()`d, and the Router then finalizes
- **THEN** the Attempt span ends with status `ERROR` and the original business error category (not `gateway_internal`)
- **AND** the Router `final_error_category` is the business error category
- **AND** the Attempt is not re-aggregated and no duplicate report is emitted

#### Scenario: Finalized-but-open attempt is not re-aggregated or re-reported

- **WHEN** a Router finalizes an Attempt whose result was already aggregated (`_aggregated_to_router = True`)
- **THEN** force-close performs no additional aggregation
- **AND** the Attempt span is reported at most once
