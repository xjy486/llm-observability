# Proposal: fix-gateway-forceclose-state-consistency

## Why

The archived change `fix-gateway-observability-closeout-followup` closed the original force-close aggregation defect, but a follow-up audit found two more **freeze-blocking state-consistency defects** in the force-close / Router-finalize state machine that the existing tests do not cover:

1. **Blocker 1 — An already-aggregated Attempt that was never `close()`d still produces a parent/child contradiction.** Today `force_close()` unconditionally marks the Attempt `gateway_internal` ERROR whenever `_error is None`, *before* checking `_aggregated_to_router`. The aggregation is correctly skipped (idempotent guard), but the span is still flipped to ERROR. So `finish_attempt(success)` + forgotten `close()` + Router finalize yields **Router OK / Attempt ERROR gateway_internal** — the exact contradiction the prior fix targeted, but for the "finalized outcome, open span" case rather than the "never finalized" case. The existing tests only cover a never-`finish_attempt`ed open Attempt.

2. **Blocker 2 — A new Attempt can still register after the Router has finalized.** The `_open_attempts_lock` added in the prior change protects dict mutation, but `register_open_attempt` does not check `Router._closed`. `Router.close()` snapshots + clears `_open_attempts` under the lock, then releases it; a concurrent `Attempt.start()` can re-insert into the now-empty dict *after* the snapshot and *after* the Router has ended — leaking an open Attempt, a non-empty registry, and an orphan reportable span. The existing concurrency tests only cover Attempt-close racing finalize, not Attempt-start/register racing finalize.

Both are correctness gaps in the very state machine the prior changes claimed to harden.

## What Changes

**Blocker 1:** `force_close()` SHALL first check `_aggregated_to_router`: if the Attempt already has a final business result aggregated, it SHALL only close the span (preserving the already-recorded OK or ERROR status) and SHALL NOT write a `gateway_internal` error or re-aggregate. Only a never-aggregated open Attempt gets the `gateway_internal` error + failure aggregation. Thus:
- finalized-success-but-open → close OK, Router stays OK;
- finalized-error-but-open → close ERROR (business error), Router stays ERROR;
- never-finalized → `gateway_internal` + aggregate → Router ERROR.

**Blocker 2:** `RouterSpan` SHALL enter a terminal `_closed` state atomically with the open-attempt snapshot+clear under the same lock, and `register_open_attempt` SHALL reject registration when `_closed` is True (returning a no-op). `RouterSpan.attempt()` and `AttemptSpan.start()` SHALL treat a closed Router as a no-op telemetry path: no registry entry, no active-attempt ContextVar, no reportable orphan span; business continues fail-open. This closes the post-finalize registration race.

Non-blocking (included opportunistically, low risk):
- The HTTP streaming-cancel E2E becomes deterministic via a server-side synchronisation event instead of `sleep`.
- Association top-level fields gain byte-length limits + control-char normalization (currently pattern-masked only).

## Capabilities

### New Capabilities
(none)

### Modified Capabilities
- `gateway-observability-runtime`: force-close preserves an already-aggregated outcome (no spurious `gateway_internal`); Router terminal state rejects post-finalize Attempt registration (no orphan span/registry leak); attempt/start no-op when the Router is closed.
- `gateway-observability-contract`: streaming-cancel E2E determinism note (non-blocking).

## Impact

- **Code:** `sdk/python/llm_observability/gateway_observability/attempt_span.py` (`force_close`, `start`), `router_span.py` (`close`, `register_open_attempt`, `attempt`).
- **Tests:** extend `test_force_close_aggregation.py` (4 finalized-but-open cases) and `test_open_attempt_cleanup.py` (4 start/register-after-close cases); make the HTTP streaming-cancel E2E deterministic; association field hardening unit tests.
- **CI:** no new jobs; all deterministic, runs in the existing `gateway-runtime-tests` job.
- **Regression:** Phase 2.1–2.5 and all archived-closeout tests remain green; no new SpanKind; telemetry stays fail-open.
