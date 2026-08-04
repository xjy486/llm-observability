# Design: fix-gateway-forceclose-state-consistency

## Context

The prior archived changes hardened the force-close path (aggregation) and the open-attempt registry (RLock). A follow-up audit found two more state-machine defects that the existing tests miss:

- `force_close()` (`attempt_span.py:329`) writes `gateway_internal` ERROR whenever `_error is None` *before* the `_aggregated_to_router` short-circuit in `_aggregate_force_close_result`. So an Attempt that called `finish_attempt(success)` (which sets `_aggregated_to_router=True`, Router success_count++) but forgot `close()` becomes ERROR at Router finalize while the Router is OK.
- `register_open_attempt` (`router_span.py:446`) does not check `_closed`; `Router.close()` snapshots+clears under the lock then releases it, so a concurrent `Attempt.start()` re-inserts after clear → leaked registry + orphan span.

Confirmed against current code (line numbers above). Both are freeze-blocking.

## Goals / Non-Goals

**Goals**
- Blocker 1: `force_close()` preserves an already-aggregated outcome (OK stays OK, business ERROR stays that error); only never-aggregated attempts get `gateway_internal`.
- Blocker 2: a closed Router rejects new Attempt registration (no orphan span/registry); `attempt()`/`start()` become a fail-open no-op telemetry path.
- Non-blocking: deterministic HTTP streaming-cancel E2E; association field length/control-char hardening.

**Non-Goals**
- No change to the streaming-finalizer or non-streaming `finalize_attempt` aggregation logic.
- No new SpanKind; no business-exception swallowing; no public API change.
- Full One-API production integration stays Phase 3.1.

## Decisions

### D1 — `force_close()` branches on `_aggregated_to_router` first (Blocker 1)

Reorder `force_close()`:
1. If `self._closed` → no-op (idempotent, unchanged).
2. If `self._aggregated_to_router` → the outcome is already in the Router; just `self.close()` (which ends the span with the status already set by `finish_attempt`/streaming — OK for success, ERROR for a recorded business error). Do NOT write `gateway_internal`, do NOT re-aggregate.
3. Else (never aggregated) → set `gateway_internal` error (only when `_error is None`), aggregate the failure result, then `close()`. (Current behavior, preserved.)

This makes the span status always agree with the already-aggregated result. The existing `_aggregate_force_close_result` idempotency guard stays as a belt-and-braces (in case `_aggregated_to_router` is somehow True but no error was set).

**Alternative:** have `force_close` inspect `self.success` instead of `_aggregated_to_router`. Rejected — `success` is derived from `_error`/`_status` and a never-finalized attempt with `upstream_status=200` would look "successful" without any result aggregated, masking the gateway-internal leak.

### D2 — Closed Router rejects registration; start/attempt no-op (Blocker 2)

`RouterSpan.close()` SHALL set `_closed=True` *inside* the `_open_attempts_lock`, in the same critical section that snapshots+clears the dict — so any `register_open_attempt` either observes `_closed` False and is in the snapshot, or observes True and is rejected. `register_open_attempt` SHALL return a bool (True if registered).

`AttemptSpan.start()` SHALL call `router.register_open_attempt(self)` and, if it returns False (Router already closed), take the **fail-open no-op telemetry path**: skip the AttemptRegistry registration, skip setting the active-attempt ContextVar, and mark the span as not-for-report (`_sampled`-style guard or simply skip `report` on close). It SHALL NOT raise. The business request continues.

`RouterSpan.attempt()` SHALL also check `_closed`: when closed, it returns an AttemptSpan flagged as no-op (so callers that never call `start()` are also safe). Belt-and-braces: the registry check in `start()` is the real guard; `attempt()`'s check avoids allocating an index/entry that can never start.

**Alternative:** raise on registration-after-close. Rejected — fail-open is a hard contract; raising would let telemetry break business.

### D3 — Deterministic HTTP streaming-cancel E2E (non-blocking)

Replace `time.sleep(0.3)` in the harness `streaming_cancel` scenario with a server-side `threading.Event` that the test sets after reading the first chunk, so the upstream generator blocks deterministically until the client disconnects. The test then asserts the wrapper's cancel finalize fired (`gateway.stream.cancelled` event present) rather than allowing OK-or-ERROR.

### D4 — Association field hardening (non-blocking)

Route `user_id`/`session_id`/`message_id`/`app_name`/`business_scene` through `set_gateway_attribute`'s length-limit + control-char path (or an equivalent bounded sanitizer), in addition to the existing `sanitize_string` pattern masking. Default byte limits: association fields ≤ 256 bytes; control chars stripped. Fail-open.

## Risks / Trade-offs

- [force_close reorder could leave a never-aggregated success-status attempt as OK] → covered by D1 step 3: never-aggregated → `gateway_internal` regardless of `_status`; only `_aggregated_to_router=True` short-circuits.
- [rejecting registration could drop a legitimately-late attempt's telemetry] → intended: a Router that has ended must not accept new attempts; the late attempt's business still runs (fail-open).
- [start() no-op path must not leave a half-constructed span] → start() checks registration result before `set_attempt`/report; on rejection it ends the span without reporting.
- [association hardening could truncate values existing tests assert verbatim] → audit; limits are generous (256B) and tests use short values.

## Migration Plan

1. Blocker 1 (force_close reorder) + tests.
2. Blocker 2 (closed-Router rejection) + tests.
3. Non-blocking: deterministic cancel E2E; association hardening.
4. Full regression + push; archive after CI green + 0-skipped.

## Open Questions
(none)
