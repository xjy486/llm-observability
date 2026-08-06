# Proposal: fix-gateway-stream-publish-before-close

## Why

The archived change `fix-gateway-router-context-weakref` made `try_aggregate_result`
publish-before-claim for the non-streaming path, but the **streaming** terminal
funnel still calls `Attempt.close()` BEFORE `try_aggregate_result(result)`. All
three streaming paths (`finalize_success`/`finalize_error`/`finalize_cancelled`)
do: `_apply_usage_to_attempt` → `_close_attempt()` → `_aggregate_to_router(result)`.

`Attempt.close()` ends + reports the Attempt span AND unregisters it from
`Router._open_attempts` and the AttemptRegistry. So a `Router.finalize()` that
races the gap between `_close_attempt()` and `_aggregate_to_router()` sees no
open Attempt (does not force-close it) and reports the Router with the
PRE-aggregate state. The resumed `_aggregate_to_router` then writes into an
already-reported Router. The reported Router Record can be `OK` while the
Attempt is `ERROR timeout`, or miss `usage.*` / `cost.*` /
`final_error_category` / final channel & status. The streaming race tests only
check post-`join()` in-memory counts (and the prior round's publish-before-claim
Record assertions covered only the non-streaming `finish_attempt` path).

Separately, a frozen main-spec text contradiction: the lifecycle requirement
says `get()` clears "the current thread's whole context" when EITHER Router or
Attempt is dead/closed — but the code (correctly) clears only the Attempt slot
when the Attempt dies while the Router is still alive (so Retry / Fallback /
next Attempt / subsequent Router events keep working). Only a dead/closed
Router clears both. The spec text must match.

## What Changes

- **Blocker — streaming publish-before-close.** Unify the three streaming
  terminal paths into one `_publish_and_close(result)` funnel:
  `try_aggregate_result(result)` → `Attempt.close()` → `Router.close()`.
  Aggregation now happens BEFORE the Attempt is unregistered/reported, so a
  racing `Router.finalize()` either still sees the open Attempt (force-closes
  it, which is a no-op re-aggregation) or sees the already-published aggregate
  — the reported Router Record always reflects the result. `finalize_success` /
  `finalize_error` / `finalize_cancelled` each build their `AttemptResult` then
  call `_publish_and_close(result)`.

- **P1 — spec text fix.** Correct the lifecycle requirement's slot-clearing
  description to: a dead/closed Router clears the whole context (Router +
  Attempt); a dead/closed Attempt while the Router is alive clears only the
  Attempt slot. This matches the code and the existing "Attempt close preserves
  the active router" scenario.

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `gateway-observability-runtime`: streaming terminal funnel is
  publish-before-close (aggregate, then close Attempt, then close Router);
  corrected slot-clearing spec text (Router-dead clears both, Attempt-dead
  clears Attempt only).

## Impact

- **Code:** `streaming.py` (`_publish_and_close`, `finalize_success/error/cancelled`).
- **Tests:** `test_stream_terminal_state.py` / new — 5 deterministic tests
  patching `try_aggregate_result`/`register_attempt_result` to block the publish
  window and asserting the REPORTED Router Record (status,
  final_error_category, usage, cost, attempt_count) for success/error/cancel.
- **CI:** deterministic, runs in the existing `gateway-runtime-tests` job.
- **Regression:** Phase 2.1–2.5 and all archived-closeout tests remain green;
  no new SpanKind; telemetry stays fail-open.
