# Proposal: fix-gateway-attempt-activation-race

## Why

The archived change `fix-gateway-forceclose-state-consistency` made `register_open_attempt` reject a closed Router, but left a window **between registration and ContextVar setup** that still leaks state, and left `attempt()` allocating an index outside the lifecycle lock. Three residual races remain:

1. **Register→set_attempt race.** `AttemptSpan.start()` registers the attempt in `_open_attempts`, then (without re-checking) calls `GatewayContext.set_attempt(self)` and records `gateway.attempt.started`. If `Router.close()` force-closes the attempt in that window, `set_attempt` re-installs `active_attempt` on a worker/thread-pool thread for an already-closed attempt, and `attempt.started` is written to an ended span. A later business `attempt.close()` hits the `if self._closed: return` early-return and **does not clear** the ContextVar it just set — a real dirty-Context leak on long-lived workers.

2. **Index-allocation race.** `RouterSpan.attempt()` checks `_closed` and then calls `allocate_attempt_index()` outside any lock. A Router that finalizes in between still bumps `_used_attempt_indices` / `_attempt_count`, so `gateway.attempt_count` can read `1` with zero Attempt spans in the trace.

3. **No-op span object.** A rejected attempt still has a `span.start()`-ed `Span` left in `self._span` (started, never ended, never reported) — a minor memory/hygiene leak.

The existing concurrency tests only loop on probability and assert registry/attempt counts; they never assert `active_attempt` is empty on the racing thread, and never use a barrier to hit the exact window.

## What Changes

- **Atomic activation + second-check.** `AttemptSpan.start()` SHALL re-check `self._closed` immediately before `GatewayContext.set_attempt`; if the attempt was force-closed between registration and here, it SHALL NOT set the ContextVar and SHALL NOT record `gateway.attempt.started`. `attempt()` SHALL check `_closed` and allocate the index under the `_open_attempts_lock` so a finalize racing allocation cannot bump `attempt_count` without a real attempt.
- **Defensive Context cleanup on early-return.** `AttemptSpan.close()` SHALL, when it finds `_closed=True` on entry, still clear the `active_attempt` ContextVar if this attempt currently owns it (fail-open), so a leaked token never survives on a worker thread.
- **No orphan span on rejection.** A rejected attempt SHALL drop its `self._span` reference (no started-but-unended Span object).
- **Deterministic barrier tests.** New tests use `threading.Event`/barriers to precisely hit "registered, before set_attempt" and "allocation racing finalize", asserting `active_attempt is None` on the racing thread, no `attempt.started` on a force-closed span, and `attempt_count` unchanged.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `gateway-observability-runtime`: atomic attempt activation (register + index under the lifecycle lock); second-check before ContextVar/event; defensive Context cleanup on `close()` early-return; no orphan span on rejection.

## Impact

- **Code:** `attempt_span.py` (`start`, `close`, new `_cleanup_context_if_owned`), `router_span.py` (`attempt` — atomic closed-check + index allocation).
- **Tests:** `test_open_attempt_cleanup.py` — 6 deterministic barrier tests.
- **CI:** deterministic, runs in the existing `gateway-runtime-tests` job.
- **Regression:** Phase 2.1–2.5 and all archived-closeout tests remain green; no new SpanKind; telemetry stays fail-open.
