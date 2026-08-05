# Design: fix-gateway-attempt-activation-race

## Context

`AttemptSpan.start()` (`attempt_span.py:116`) does, in order: create+start Span → AttemptRegistry.register → `router.register_open_attempt(self)` → `GatewayContext.set_attempt(self)` → record `attempt.started`. After the prior fix, `register_open_attempt` rejects a closed Router — but if registration *succeeds* and the Router then finalizes before `set_attempt`, `force_close` ends the attempt (sets `_closed=True`, cleans the registry) and the resumed `start()` re-installs `active_attempt` on an ended attempt and writes `attempt.started` to an ended span. `close()`'s `if self._closed: return` then skips ContextVar cleanup → leaked token on a worker thread.

Separately, `RouterSpan.attempt()` (`router_span.py:626`) reads `_closed` and calls `allocate_attempt_index()` outside any lock, so a racing finalize leaves `attempt_count` bumped with no real attempt. And a rejected attempt keeps a started `Span` in `self._span`.

Confirmed against current code (line numbers above). All three are freeze-blocking concurrency defects.

## Goals / Non-Goals

**Goals**
- Close the register→set_attempt window: no ContextVar/event on an attempt that was force-closed mid-activation.
- Make `attempt()` index allocation atomic with the closed-check (no count bump without a real attempt).
- Defensive: `close()` early-return still clears a token it owns.
- No started-but-unended Span on a rejected attempt.
- Deterministic barrier tests asserting the exact window and `active_attempt is None` on the racing thread.

**Non-Goals**
- No change to aggregation / streaming / cost logic.
- No single global lifecycle lock redesign (keep the two existing locks; nest open_attempts→index consistently).
- No new SpanKind; no business-exception swallowing.

## Decisions

### D1 — Second-check before `set_attempt` (race 1)

In `start()`, after `register_open_attempt` returns True, re-check `self._closed` immediately before `GatewayContext.set_attempt`. If True (the attempt was force-closed between registration and here), return without setting the ContextVar and without recording `attempt.started`. The attempt is already ended + reported by `force_close`; `start()` only needs to avoid re-installing state. This is the primary fix — it prevents the leak at the source.

**Alternative:** make `register_open_attempt` + `set_attempt` a single atomic critical section. Rejected — `set_attempt` touches a ContextVar (per-context state) that doesn't belong under the Router's lock, and holding the lock across span/event work risks lock-ordering issues with the reporter.

### D2 — `close()` clears owned token on early-return (defense-in-depth)

Add `_cleanup_context_if_owned()`: if `self._ctx_token` is set AND `GatewayContext.get().active_attempt is self`, clear it. Call this in the `if self._closed: return` path of `close()`. With D1 the race no longer sets the token, but this guarantees no leaked token survives even if a future path sets one after close.

### D3 — Atomic closed-check + index allocation in `attempt()` (race 2)

`RouterSpan.attempt()` SHALL acquire `_open_attempts_lock`, check `_closed`, and (if open) call `allocate_attempt_index` while still holding the lock (nested `_index_lock` acquisition; ordering open_attempts→index is consistent and never reversed elsewhere). If closed, return a no-op AttemptSpan without allocating. This makes `_attempt_count`/`_used_attempt_indices` only move when a real attempt can be activated.

### D4 — Drop the orphan span on rejection (hygiene)

In `start()`'s rejection path (register returned False), set `self._span = None` after removing the registry entry, so no started-but-unended Span object lingers. The span was never reported (`_no_op`); dropping the reference lets it be collected.

## Risks / Trade-offs

- [Second-check reads `_closed` without a lock] — a bool read is sufficiently atomic for this best-effort guard; the worst case is a single missed re-check, which D2's defensive cleanup backstops.
- [nested lock open_attempts→index] — `allocate_attempt_index` only takes `_index_lock`; `register_open_attempt` only takes `_open_attempts_lock`; no path takes index→open_attempts, so no deadlock.
- [barrier tests patch `register_open_attempt`] — the patch calls the original then blocks on an event, deterministically hitting the window without busy-loops.

## Migration Plan

1. D1 + D2 (start second-check + close defensive cleanup) + tests.
2. D3 (atomic attempt()) + count test.
3. D4 (drop orphan span) + test.
4. Full regression + push; archive after CI green + 0-skipped.

## Open Questions
(none)
