# Proposal: fix-gateway-router-context-weakref

## Why

The archived change `fix-gateway-active-attempt-weakref` made the
`active_attempt` slot a weak reference with lazy invalidation, but left two
gaps the audit found:

1. **P0-1 — the Router slot is still a strong reference and is not lazily
   invalidated.** `GatewayContextState.router` holds the Router strongly; `get()`
   only invalidates the Attempt slot. After a cross-thread `Router.finalize()`
   sets `_closed=True`, the owner thread's `active_router` still returns the
   ended Router (`Runtime.active_router()` returns it). Worse, the strong
   Router slot transitively pins `Router._attempts` → all Attempts in memory,
   so the weak-attempt fix does not actually let them be collected while the
   Router slot lives on a reused worker thread. The existing thread-pool test
   only calls `attempt.force_close()` (not `handle.finalize()`) and only
   asserts `active_attempt`, never `active_router` — so it sidesteps the bug.

2. **P0-2 — `try_aggregate_result` has a claim-before-publish race.** It sets
   `_aggregated_to_router=True` and releases the Attempt lifecycle lock BEFORE
   calling `Router.register_attempt_result(result)`. A `Router.finalize()` that
   runs in that gap sees `_aggregated_to_router=True`, assumes the result is
   already published, ends + reports the Router with the PRE-gap aggregate —
   then the resumed `register_attempt_result` writes into an already-reported
   Router. The reported Router Record can miss `usage.*` / `cost.*` /
   `final_error_category` / counts, or disagree with the final in-memory state.
   The existing race tests only check post-`join()` in-memory counts (and one
   uses `assert total <= 1`, which lets `total == 0` pass), so they do not catch
   a result published after the Router report.

3. **P1 — public API regression.** `GatewayContextState.active_attempt` now
   returns an `ActiveAttemptRef` (was an `AttemptSpan`/None). `GatewayContextState`
   and `get_gateway_context` are exported public API. A `@property` should keep
   the old contract (returns the live Attempt or None) while the weak field stays
   private.

## What Changes

- **P0-1 — Router slot weak + lazy invalidation.** `GatewayContextState` holds
  the Router behind a weak ref too (`_router_ref`), exposed via a `router`
  property that dereferences lazily. `GatewayContext.get()` invalidates BOTH
  slots: a dead or `_closed` Router clears the current thread's context (both
  slots) and reads None. `Runtime.active_router()` never returns an ended
  Router. This releases the transitive Attempt pinning.

- **P0-2 — publish-before-claim.** `try_aggregate_result` SHALL call
  `Router.register_attempt_result(result)` INSIDE the `_lifecycle_lock`
  critical section, BEFORE setting `_aggregated_to_router=True`. So a
  `Router.finalize()` that observes `_aggregated_to_router=True` is guaranteed
  the result is already in the Router aggregate (no report missing it). Lock
  ordering is fixed: `Attempt._lifecycle_lock` → `Router._aggregate_lock`,
  never reversed (audited: no path takes aggregate→lifecycle).

- **P1 — public-API-safe property.** `GatewayContextState.active_attempt` /
  `router` become `@property` dereferencing the private weak fields, preserving
  the historical contract (`AttemptSpan`/`RouterSpan` or None).

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `gateway-observability-runtime`: Router slot weak + lazy invalidation (no
  cross-thread Router-context leak, no transitive Attempt pinning);
  publish-before-claim aggregation (reported Router Record matches the final
  in-memory aggregate); public-API-safe dereferencing properties.

## Impact

- **Code:** `context.py` (weak Router slot + lazy `get` + properties),
  `attempt_span.py` (`try_aggregate_result` publish-before-claim), `runtime.py`
  (`active_router` deref).
- **Tests:** `test_open_attempt_cleanup.py` — cross-thread Router-finalize +
  thread-pool-reuse `active_router` tests (calling `handle.finalize()`);
  `test_parallel_aggregation.py` — publish-before-claim tests asserting the
  REPORTED Router Record (not just in-memory counts), with `assert == 1`.
- **CI:** deterministic, runs in the existing `gateway-runtime-tests` job.
- **Regression:** Phase 2.1–2.5 and all archived-closeout tests remain green;
  no new SpanKind; telemetry stays fail-open.
