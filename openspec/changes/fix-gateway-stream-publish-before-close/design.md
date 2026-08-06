# Design: fix-gateway-stream-publish-before-close

## Context

`_TerminalFinalizer.finalize_success/error/cancelled` (`streaming.py:184/210/241`)
each do `_apply_usage_to_attempt()` → `_close_attempt()` → `_aggregate_to_router(result)`
→ `_close_router()`. `_close_attempt()` calls `Attempt.close()`, which ends +
reports the Attempt span AND unregisters it from `Router._open_attempts` +
the AttemptRegistry. So between `_close_attempt()` and `_aggregate_to_router()`
the Attempt is no longer tracked, yet the result is not yet in the Router
aggregate. A `Router.finalize()` racing that gap sees no open Attempt and reports
the Router with the stale aggregate; the resumed aggregation writes into an
already-reported Router → reported Record disagrees with final state.

Confirmed against current code (line numbers above). The non-streaming path
was fixed in `fix-gateway-router-context-weakref` (publish-before-claim); the
streaming path was missed.

Separately, the frozen lifecycle spec text says `get()` clears "the whole
context" when EITHER slot is dead/closed, but the code clears only the Attempt
slot when the Attempt dies while the Router is alive. The code is correct
(Router must survive an Attempt close for Retry/Fallback); the spec text is
wrong.

## Goals / Non-Goals

**Goals**
- Streaming terminal states publish the result to the Router BEFORE closing
  the Attempt (and thus before unregistering it / reporting the Router).
- One `_publish_and_close(result)` funnel replaces the 3 duplicated
  close→aggregate orderings.
- Correct the slot-clearing spec text to match the code (Router-dead ⇒ both;
  Attempt-dead-while-Router-alive ⇒ Attempt only).

**Non-Goals**
- No change to aggregation semantics (which error wins, idempotency).
- No change to the non-streaming path (already publish-before-claim).
- No new SpanKind; no business-exception swallowing.

## Decisions

### D1 — `_publish_and_close(result)` funnel (Blocker)

```
def _publish_and_close(self, result: AttemptResult):
    self._aggregate_to_router(result)   # publish FIRST (try_aggregate_result,
                                         # publish-before-claim, under _lifecycle_lock)
    self._close_attempt()               # then end+report Attempt, unregister
    self._close_router()                # then end+report Router
```

Each of `finalize_success/error/cancelled` builds its `AttemptResult` (with the
already-captured `usage`/`error`/`cost`/`status`) then calls `_publish_and_close`.
Because `try_aggregate_result` is publish-before-claim under `_lifecycle_lock`,
a racing `Router.finalize()` either (a) sees the Attempt still open and
force-closes it — `force_close`'s `try_aggregate_result` is then a no-op (already
aggregated) — or (b) runs after the publish and sees the published aggregate.
Either way the Router Report includes the result. The Attempt is unregistered
only AFTER the publish, so the "finalize sees no open Attempt" gap is gone.

**Why not move `_close_router` inside the lock too?** Not needed — once the
result is published, the Router's `_apply_aggregates` reads a consistent
snapshot under `_aggregate_lock`; the Router close racing is fine.

### D2 — spec text fix (P1)

Rewrite the lifecycle requirement's `get()` description to:
- a dead/closed Router → clear the current thread's whole context (both slots);
- a dead/closed Attempt while the Router is alive → clear ONLY the Attempt slot.

This matches the code (`context.get()`) and the existing "Attempt close
preserves the active router" scenario. No code change.

## Risks / Trade-offs

- [publishing before closing the Attempt span] — the Attempt span is still open
  during publish; `register_attempt_result` reads `attempt.channel_id` etc.
  from the AttemptResult (already captured), not the live Attempt, so this is
  safe. The span end happens in `_close_attempt` after publish — consistent.
- [force_close racing the publish] — `force_close` calls
  `try_aggregate_result` (no-op if already aggregated) then `close()` (idempotent
  via `_closed`). No double-report.
- [streaming cancel partial usage] — `_apply_usage_to_attempt` still runs before
  `_publish_and_close` to capture partial usage into the result; only the close
  order changes.

## Migration Plan

1. D1 (`_publish_and_close`, rewrite the 3 finalize paths) + 5 tests asserting
   the REPORTED Router Record under a publish-window race.
2. D2 (spec text fix — no code).
3. Full regression + push; archive after CI green + 0-skipped.

## Open Questions
(none)
