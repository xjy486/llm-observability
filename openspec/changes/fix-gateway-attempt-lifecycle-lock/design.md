# Design: fix-gateway-attempt-lifecycle-lock

## Context

`AttemptSpan.start()` (`attempt_span.py:196`) checks `if self._closed: return`
then — with no lock held — calls `GatewayContext.set_attempt` and records
`attempt.started`. `force_close()` (`attempt_span.py:386`) sets `_closed` and
ends the span but cannot clear a ContextVar not yet installed. So a finalize
landing in that gap leaves a leaked token + a late event on an ended span;
`close()`'s defensive cleanup only runs if the business thread calls it later.
The barrier tests added by `fix-gateway-attempt-activation-race` pause before
the closed-check, so they pass through the second-check — they never exercise
the final window.

Separately `RouterSpan.register_attempt_result` (`router_span.py:715`) mutates
`_success_count`/`_fail_count`/`_final_error`/`_usage_aggregate`/
`_cost_aggregate`/`_final_channel_id` with no lock; parallel attempts
(hedged / concurrent provider) race.

Confirmed against current code (line numbers above).

## Goals / Non-Goals

**Goals**
- Make the post-activation Context-install + event-record atomic with
  `force_close`/`close` via a per-Attempt `RLock`.
- Prove the final window is closed with tests that block *inside*
  `set_attempt`, and assert no token survives without a business `close()`.
- Make parallel `register_attempt_result` race-free via a Router
  `_aggregate_lock`.

**Non-Goals**
- No single global lifecycle lock; keep per-Attempt + per-Router locks.
- No change to aggregation *semantics* (which attempt's error wins, etc.) —
  only concurrency.
- No new SpanKind; no business-exception swallowing.

## Decisions

### D1 — Per-Attempt `_lifecycle_lock` (Blocker)

Add `self._lifecycle_lock = threading.RLock()` on `AttemptSpan`. Extract the
post-activation confirmation into:

```
def _activate_context_and_started_event(self) -> bool:
    with self._lifecycle_lock:
        if self._closed or self._no_op:
            return False
        self._ctx_token = GatewayContext.set_attempt(self)
        if self._closed:              # finalize raced inside the CS
            self._cleanup_context_if_owned()
            return False
        self.recorder.attempt_started(...)
        return True
```

`force_close()` and `close()` take `_lifecycle_lock` for their `_closed`-state
transition. RLock so `close()` → `_cleanup_context_if_owned` (and `force_close`
→ `close`) re-enter safely. Only two interleavings are now possible:
- `start` wins the lock → Context + event fully installed → `force_close`
  runs after (clears the token it now can see).
- `force_close` wins → `_closed=True` → `start`'s CS returns False, installs
  nothing.

No "check passed, finalize lands before install" middle state. The
post-install re-check inside the CS covers the rare case finalize lands
*between* `set_attempt` and the event (then we clear the just-installed token
and skip the event).

**Alternative:** a third lockless check. Rejected (per the report) — N
lockless checks still leave N-1 windows; only a lock closes it.

### D2 — Router `_aggregate_lock` (P1)

Add `self._aggregate_lock = threading.RLock()` on `RouterSpan`. Wrap the entire
`register_attempt_result` mutation (counts, final_error, ttft, usage, cost,
final_channel) in it. `set_usage_aggregate`/`set_cost_aggregate` (cache-hit
direct setters) also take it for consistency. `_apply_aggregates` (called under
`close()`) reads the aggregates — take the lock there too for a consistent
snapshot, or accept a benign torn read at the very end (chose: take the lock;
it's one read at finalize).

### D3 — Tests that hit the real window

Patch `GatewayContext.set_attempt` to block *after* entering the method
(i.e. the attempt already passed the closed-check and is inside the CS). This
proves the lock holds: finalize cannot complete until `set_attempt` returns,
and the post-install re-check clears the token. Assert `active_attempt is None`
and no `attempt.started` on the ended span — without ever calling `close()` on
the business thread (the owner-never-closes test).

## Risks / Trade-offs

- [Holding `_lifecycle_lock` across `set_attempt`] — `set_attempt` is a fast
  ContextVar set; holding the lock is brief. The post-install re-check is the
  cost of full atomicity.
- [Nested `_lifecycle_lock` and `_open_attempts_lock`] — `force_close` is
  called by `_force_close_open_attempts` which holds `_open_attempts_lock`;
  `force_close` takes `_lifecycle_lock`. No path takes
  `_lifecycle_lock`→`_open_attempts_lock`, so no deadlock (ordering:
  open_attempts → lifecycle).
- [`_aggregate_lock` under heavy parallelism] — aggregation is O(1) per call;
  contention is bounded by attempt count (small).

## Migration Plan

1. D1 (`_lifecycle_lock`, `_activate_context_and_started_event`, `force_close`,
   `close`) + 3 window tests.
2. D2 (`_aggregate_lock`, wrap `register_attempt_result` + setters) + 4
   parallel tests.
3. Full regression + push; archive after CI green + 0-skipped.

## Open Questions
(none)
