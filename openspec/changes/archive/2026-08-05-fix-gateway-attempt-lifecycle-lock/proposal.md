# Proposal: fix-gateway-attempt-lifecycle-lock

## Why

The archived change `fix-gateway-attempt-activation-race` added a lockless
second-check before `set_attempt`, but a follow-up audit found that the window
between that check and `GatewayContext.set_attempt` is still non-atomic, and
that parallel attempt-result aggregation has no lock at all. Two defects:

1. **Blocker — the closed-check → set_attempt window is still open.**
   `start()` reads `if self._closed: return`, then (without holding any lock)
   calls `set_attempt` + records `attempt.started`. If `force_close` runs in
   that gap, it sets `_closed`, ends + reports the span, but cannot clear a
   ContextVar that hasn't been installed yet. The resumed `start()` then
   installs `active_attempt` on a long-lived worker thread for an already-ended
   attempt and writes a late `attempt.started` on an ended span. The defensive
   `close()` cleanup only fires if the business thread later calls `close()`;
   on abnormal Router-exit / request-cancel paths it never does, so the token
   leaks. The existing barrier tests pause *before* the closed-check, so they
   never hit this final window — they cannot prove it is closed.

2. **P1 — `register_attempt_result` is not thread-safe.** With hedged /
   parallel provider attempts, two concurrent aggregations race on
   `_success_count`/`_fail_count` (lost updates), `_usage_aggregate`/
   `_cost_aggregate` (read-modify-write overwrites), and `_final_error`/
   `_final_channel_id` (unstable). The runtime already supports concurrent
   attempts; aggregation must too.

## What Changes

- **Attempt lifecycle lock (Blocker).** Add `AttemptSpan._lifecycle_lock =
  threading.RLock()`. The post-activation confirmation — re-check `_closed`,
  install the ContextVar, record `attempt.started` — runs as one critical
  section in `_activate_context_and_started_event()`. `force_close()` and
  `close()` take the same lock for their state transition, so only two orders
  are possible: `start` completes the Context+event then `force_close` runs, or
   `force_close` closes first and `start`'s critical section observes `_closed`
   and installs nothing. No third window. No business-thread `close()` required
   to clean up.
- **Aggregate lock (P1).** Add `RouterSpan._aggregate_lock = threading.RLock()`
  and wrap the whole `register_attempt_result` state mutation in it, so
  parallel attempt results aggregate without lost updates.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `gateway-observability-runtime`: Attempt lifecycle lock making activation
  atomic with force_close/close; Router aggregate lock on
  `register_attempt_result`.

## Impact

- **Code:** `attempt_span.py` (`_lifecycle_lock`, `_activate_context_and_started_event`,
  `force_close`, `close`, `start`), `router_span.py` (`_aggregate_lock`,
  `register_attempt_result`).
- **Tests:** `test_open_attempt_cleanup.py` — 3 deterministic tests that patch
  `GatewayContext.set_attempt` to block *inside* the final window; 4 parallel
  aggregation tests.
- **CI:** deterministic, runs in the existing `gateway-runtime-tests` job.
- **Regression:** Phase 2.1–2.5 and all archived-closeout tests remain green;
  no new SpanKind; telemetry stays fail-open.
