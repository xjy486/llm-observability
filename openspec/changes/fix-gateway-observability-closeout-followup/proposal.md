# Proposal: fix-gateway-observability-closeout-followup

## Why

The archived change `fix-gateway-native-observability-closeout` passed its three evidence layers (code review, CI green, Real E2E 0-skipped), but a follow-up audit of the frozen code found two **freeze-blocking correctness defects** and four **P1 hardening gaps** that the closeout's own adversarial tests did not catch:

1. **Blocker 1 — Force-closed attempts are not aggregated to the Router.** `AttemptSpan.force_close()` marks the Attempt ERROR and ends its span, but never calls `router.register_attempt_result(...)`. So a Router that finalizes with an open Attempt ends with `fail_count = 0`, `final_error = None`, status `OK`, and records `gateway.response.completed` — while its child Attempt is `ERROR gateway_internal`. That is exactly the parent/child terminal-state contradiction the rework forbade. The closeout's `test_open_attempt_cleanup.py` only asserts the Attempt is closed/ERROR and the registry is empty; it never asserts the Router must be ERROR.

2. **Blocker 2 — The unified privacy entry point is not actually wired in.** `set_gateway_attribute(...)` exists with the correct pipeline (whitelist, secret masking, URL-query stripping, length limits, type normalization, fail-open), but Router and Attempt still write external strings (`request_id`, `route`, `provider`, `resolved_model`, `route_reason`, `policy_name`, `upstream_request_id`, `error_message`, `finish_reason`, …) via raw `span.set_attribute(...)`. The closeout tests `set_gateway_attribute` on a bare hand-made span, never through the real `GatewayRuntime → RouterSpan/AttemptSpan` path with a malicious value.

3. **P1-1 — Streaming never computes Cost.** Non-streaming attempts call `CostCalculator.calculate(usage, model=attempt.resolved_model)`; the streaming `_TerminalFinalizer` only receives a `UsageNormalizer` and builds `AttemptResult(usage=...)` with `cost=None` always. With a pricing table configured, non-streaming attempts carry `cost.*` and streaming attempts do not.

4. **P1-2 — Generic streaming exceptions classify as `unknown`.** `_classify()` calls the global `classify_error()`, which only recognizes `TimeoutError`/`ConnectionError`/some `OSError`; a stream parse error, protocol error, or generator anomaly falls through to `gateway.error_category = unknown`. The streaming contract prefers `stream_interrupted` for an unclassifiable mid-stream interruption.

5. **P1-3 — Real E2E still stops at the Runtime, not the server layer.** The live test drives `runtime.handle_request(...)` directly and replaces `tracer.reporter.report` in memory. It does not exercise a real HTTP client → gateway middleware/adapter → Runtime → upstream → Reporter HTTP → Core ingest API. The One-API glue layer is therefore unproven.

6. **P1-4 — The open-attempt registry has no concurrency guard.** `attempt_index` allocation is locked, but `_open_attempts: dict` register/unregister/snapshot/force-close/clear are not. Parallel attempts, hedged requests, or Router-finalize racing Attempt-close can miss snapshots or race.

This change fixes all six on top of the archived (immutable) closeout, without modifying archived history.

## What Changes

**Blockers (freeze-blocking):**

- **Blocker 1:** `force_close()` SHALL construct the terminal `AttemptResult(error=<gateway_internal error>, success=False, usage=<attempt usage>, cost=<attempt cost>)` and aggregate it into the Router exactly once (idempotent via the existing `_aggregated_to_router` guard), so a Router finalizing with an open Attempt ends `ERROR` with `gateway.final_error_category = gateway_internal`, records `gateway.response.failed` exactly once, and increments `fail_count`. Business errors already recorded on the Attempt are preserved (never overwritten).
- **Blocker 2:** Router and Attempt SHALL route all external-string span attributes through `set_gateway_attribute(span, key, value, self._privacy)`. Internal counters, booleans, hashed channel IDs, and numeric metrics may continue to use `set_attribute` directly. New runtime-integration tests drive malicious values through the real `GatewayRuntime`/`RouterSpan`/`AttemptSpan` path.

**P1 hardening:**

- **P1-1:** The streaming finalizer SHALL receive the `CostCalculator` and the Attempt's `resolved_model` and SHALL compute `Attempt` Cost from the captured terminal Usage (fail-open), writing it to the Attempt and the Router cost aggregate. Streaming and non-streaming attempts SHALL both carry `cost.*` when a pricing table is configured.
- **P1-2:** In the streaming terminal funnel only, an `unknown` classified error SHALL be mapped to `stream_interrupted`. Global `classify_error()` behavior is unchanged.
- **P1-3:** A minimal real HTTP gateway harness (ASGI/aiohttp) and a real Mock Core HTTP server (`/api/v1/ingest`) SHALL exercise the full chain: HTTP client → gateway middleware/adapter → GatewayRuntime → Router/Attempt → mock upstream → Reporter HTTP → Core ingest API. The existing runtime-level live-upstream test is retained (renamed to reflect its scope) as a separate, secret-gated live test.
- **P1-4:** The Router's `_open_attempts` registry SHALL be protected by an independent `RLock` covering register, unregister, snapshot, force-close iteration, and clear, so concurrent attempts and Router-finalize/Attempt-close races cannot miss or double entries.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `gateway-observability-runtime`: force-close aggregation semantics; `set_gateway_attribute` enforced wiring; streaming Cost computation; streaming `unknown → stream_interrupted` classification; real HTTP E2E harness requirement; open-attempt registry concurrency lock.
- `gateway-observability-contract`: streaming Cost ownership; streaming generic-error classification scenario; streaming-terminal Router/Attempt consistency for force-closed attempts.

## Impact

- **Code:** `sdk/python/llm_observability/gateway_observability/` (`attempt_span.py`, `router_span.py`, `streaming.py`, `runtime.py`, `privacy.py`/`attributes.py` only if needed), `sdk/tests/gateway_observability/` (new + extended tests, new HTTP harness helper module).
- **New tests:** `test_force_close_aggregation.py` (Blocker 1), `test_runtime_privacy_integration.py` (Blocker 2), streaming-cost + streaming-classification additions to `test_stream_terminal_state.py` (P1-1/P1-2), `test_gateway_http_e2e.py` + `gateway_http_harness.py` helper (P1-3), concurrency additions to `test_open_attempt_cleanup.py` (P1-4).
- **CI:** the `gateway-real-e2e` job already sources from `secrets.E2E_*` under unified `GATEWAY_E2E_*` names; the new HTTP E2E is deterministic (mock upstream) and runs in the always-on `gateway-runtime-tests` job, not the secret-gated one.
- **Regression constraint:** Phase 2.1–2.5 and the archived closeout's tests must remain green; no new SpanKind; no business-exception swallowing; telemetry stays fail-open.
