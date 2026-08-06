# Proposal: fix-gateway-active-attempt-weakref

## Why

The archived change `fix-gateway-attempt-lifecycle-lock` closed the
closed-check→set_attempt window with a lifecycle lock + intent flag, but a
follow-up audit found two residual defects it explicitly could not close:

1. **Blocker — cross-thread force_close after full activation cannot clean the
   owner's ContextVar.** Python `ContextVar`s are per-Context/per-thread. Once
   an Attempt has fully activated (ContextVar installed + started recorded +
   lock released) and the owner thread then never calls `close()`, a later
   `Router.finalize()` on a *different* thread ends the span and clears the
   registry but CANNOT reset the owner thread's `active_attempt` —
   `clear_attempt(token)` resets only the calling thread's Context, and
   `reset(token)` raises `ValueError` for a foreign token. The `_closing`
   intent flag does not help: force_close starts *after* activation completed,
   so there is no in-flight force-close for the activation critical section to
   observe. Result: a long-lived worker / thread-pool thread retains a stale
   `active_attempt` pointing at an ended Attempt. This is the most realistic
   leak (thread-pool reuse).

2. **P1 — the per-Attempt aggregation guard is not under the lifecycle lock.**
   `runtime.finalize_attempt`, the streaming finalizer, and
   `_aggregate_force_close_result` all do `if not _aggregated_to_router:
   _aggregated_to_router = True; register_attempt_result(...)` without holding
   `_lifecycle_lock`. The Router `_aggregate_lock` only serializes the two
   calls; it does not prevent the *same* Attempt from being aggregated twice
   when `finish_attempt` races `force_close` (both see `False`, both aggregate).

## What Changes

- **Blocker — weakref active-attempt with lazy invalidation.** Change
  `GatewayContextState.active_attempt` from a strong reference to a
  `weakref.ref` (or an `ActiveAttemptRef` holding one). `GatewayContext.get()`
  dereferences lazily: if the referent is dead OR `.closed`, the current
  thread's attempt slot is cleared and `active_attempt` reads `None`.
  `Runtime.active_attempt()` never returns a closed/ended Attempt. This makes
  cross-thread cleanup unnecessary — the ContextVar stops surfacing an ended
  Attempt the moment it is read again, on whichever thread reads it. No
  reliance on the owner calling `close()`.

- **P1 — single aggregated `try_aggregate_result`.** Add
  `AttemptSpan.try_aggregate_result(result) -> bool` that performs the
  `_aggregated_to_router` check-and-set atomically under `_lifecycle_lock`,
  then calls `router.register_attempt_result(result)` (under the Router's
  `_aggregate_lock`). Route `runtime.finalize_attempt`, the streaming
  finalizer, and `_aggregate_force_close_result` all through it, so the same
  Attempt is aggregated at most once regardless of which path wins.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `gateway-observability-runtime`: weakref active-attempt with lazy
  invalidation (no cross-thread ContextVar leak after full activation); single
  `try_aggregate_result` funnel for exactly-once aggregation across all paths.

## Impact

- **Code:** `context.py` (`GatewayContextState` weakref, `get` lazy clear,
  `set_attempt`/`clear_attempt`), `attempt_span.py` (`try_aggregate_result`),
  `runtime.py` + `streaming.py` (route through `try_aggregate_result`).
- **Tests:** `test_open_attempt_cleanup.py` — cross-thread full-activation +
  thread-pool-reuse tests; `test_parallel_aggregation.py` / new — exactly-once
  aggregation race tests.
- **CI:** deterministic, runs in the existing `gateway-runtime-tests` job.
- **Regression:** Phase 2.1–2.5 and all archived-closeout tests remain green;
  no new SpanKind; telemetry stays fail-open.
