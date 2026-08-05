# Delta: gateway-observability-runtime

本 delta 处理归档 forceclose-state-consistency 之后的 3 个残余并发缺陷（register→set_attempt 竞态泄漏 ContextVar / attempt() 序号分配竞态 / no-op Span 残留），不修改已归档历史。

## MODIFIED Requirements

### Requirement: RouterSpan and AttemptSpan lifecycle and registries

The runtime SHALL provide `RouterSpan` and `AttemptSpan` runtime objects whose spans remain open from request start until a terminal state. The runtime SHALL maintain per-request Router and Attempt registries and clean them on success, error, client cancel, generator close, async generator aclose, and span-end failure. No ContextVar, Registry entry, streaming wrapper reference, or HTTP session handle SHALL leak after any terminal path.

The Router SHALL explicitly track its open Attempts (`_open_attempts: dict[str, AttemptSpan]`): an Attempt registers on start and unregisters on close. The `_open_attempts` registry SHALL be protected by an independent re-entrant lock covering registration, unregistration, snapshot, force-close iteration, and clear, so concurrent attempts, hedged requests, or a Router-finalize racing an Attempt-close cannot miss or double an entry. Router `finalize()` SHALL force-close every remaining open Attempt via an idempotent, fail-open `force_close(category="gateway_internal", reason="router_finalized_with_open_attempt")`. Merely deleting registry entries without ending the spans is forbidden. `force_close()` SHALL NOT overwrite an already-recorded business error, SHALL clean the registry and context entries, and SHALL report the final span. After Router finalize, the open-attempt registry SHALL be empty and no attempt SHALL be reported twice.

Once a Router has reached its terminal state (`_closed = True`), it SHALL reject registration of any new Attempt: `register_open_attempt` SHALL be a no-op that returns False when the Router is closed, and `RouterSpan.attempt()` / `AttemptSpan.start()` SHALL treat a closed Router as a fail-open no-op telemetry path — no registry entry, no active-attempt ContextVar, no reportable orphan span — while business continues. The Router SHALL set `_closed = True` atomically with the open-attempt snapshot+clear under the same lock, so a concurrent register either is captured in the snapshot or observes the closed state and is rejected. `RouterSpan.attempt()` SHALL check `_closed` and allocate the Attempt index under the lifecycle lock, so a Router that finalizes during allocation cannot bump `attempt_count` / `_used_attempt_indices` without a real, activatable Attempt; `gateway.attempt_count` SHALL always equal the number of real Attempt spans.

`AttemptSpan.start()` SHALL treat activation as a critical section: after `register_open_attempt` succeeds, it SHALL re-check `self._closed` immediately before setting the active-attempt ContextVar and recording `gateway.attempt.started`. If the Attempt was force-closed between registration and that point, it SHALL NOT set the ContextVar and SHALL NOT record the started event — the Attempt is already ended + reported by `force_close`. `AttemptSpan.close()` SHALL, when it finds `_closed = True` on entry, still clear the `active_attempt` ContextVar if this Attempt currently owns it (fail-open), so a leaked token never survives on a worker/thread-pool thread. A rejected (no-op) Attempt SHALL drop its `Span` reference so no started-but-unended Span object lingers.

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
