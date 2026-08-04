# Design: fix-gateway-observability-closeout-followup

## Context

The archived closeout (`fix-gateway-native-observability-closeout`) froze the Phase 3 contract/runtime and is immutable. A follow-up audit found two freeze-blocking defects that the closeout's tests did not catch (force-close not aggregated; privacy guard not wired) and four P1 gaps (streaming cost; `unknown` streaming classification; server-level E2E; registry concurrency). All six are confirmed against the current code:

- `AttemptSpan.force_close()` (`attempt_span.py`): calls `set_error` + `close()` only; never `register_attempt_result`.
- `RouterSpan.start()` / `AttemptSpan.start()/set_*`: external strings via raw `span.set_attribute(...)`.
- `_TerminalFinalizer.__init__` (`streaming.py`): takes `usage_normalizer` only; `AttemptResult.cost` always None.
- `_TerminalFinalizer._classify`: keeps `classify_error()`'s `UNKNOWN` (maps to `STREAM_INTERRUPTED` only when the classifier itself raises).
- `test_real_gateway_e2e.py`: drives `runtime.handle_request()` directly; Mock Core replaces `reporter.report` in memory.
- `RouterSpan._open_attempts: dict` (`router_span.py`): only `_index_lock` exists; registry mutations are unlocked.

Constraint: do not modify archived history; keep all existing tests green; telemetry stays fail-open; no new SpanKind; no business-exception swallowing.

## Goals / Non-Goals

**Goals**

- Fix Blocker 1 so a force-closed Attempt makes its Router `ERROR` (consistent parent/child terminal state) — exactly-once aggregation, business errors preserved.
- Fix Blocker 2 so the real Router/Attempt path cannot write an unguarded external string, proven by runtime-integration tests with malicious inputs.
- P1-1: streaming attempts carry `cost.*` like non-streaming ones.
- P1-2: an unclassifiable streaming interruption is `stream_interrupted`, not `unknown` (streaming funnel only).
- P1-3: a real HTTP gateway harness + real Mock Core HTTP server prove the full chain end-to-end (deterministic, mock upstream).
- P1-4: the open-attempt registry is race-free under concurrent attempts and finalize/close races.

**Non-Goals**

- No redesign of the Phase 3 architecture; no new SpanKind; no One-API route-behavior changes.
- P1-3 builds a *minimal* harness sufficient to prove the glue layer; it is not a production gateway. Full One-API integration remains Phase 3.1.
- The live-upstream test stays secret-gated and separate; the new HTTP E2E uses a mock upstream so it is deterministic and not secret-gated.
- Global `classify_error()` behavior is unchanged; only the streaming funnel remaps `unknown`.

## Decisions

### D1 — `force_close()` is the single aggregation funnel for force-closed attempts (Blocker 1)

`force_close()` already marks the error and closes the span. It SHALL additionally construct the terminal `AttemptResult` and aggregate it into the Router (idempotent via the existing `_aggregated_to_router` guard, mirroring the streaming finalizer's pattern). This keeps the Router's `_final_error` / `_fail_count` correct so that the subsequent `Router.close()` → `_record_terminal_event()` emits `gateway.response.failed` and sets status `ERROR`. Business errors already on the Attempt are preserved (`force_close` only sets `gateway_internal` when `_error is None`). The Attempt's captured usage/cost (if any) are carried into the result so partial consumption still aggregates.

**Alternative considered:** aggregate in `Router._force_close_open_attempts()` after calling `force_close()`. Rejected — it splits the terminal-state construction across two objects and risks the Router overwriting an error the Attempt already owns. `force_close()` owning the full funnel matches the streaming finalizer's single-funnel design.

### D2 — Route external strings through `set_gateway_attribute` (Blocker 2)

Add a small private helper on Router/Attempt, `_set_attr(span, key, value)`, that delegates to `set_gateway_attribute(span, key, value, self._privacy)`. Replace direct `span.set_attribute(...)` calls for external-string keys (`request_id`, `route`, `provider`, `resolved_model`, `requested_model`, `route_reason`, `policy_name`, `channel_type`, `version`, `gateway.name`, `protocol`, `upstream_request_id`, `error_type`, `error_message`, `finish_reason`). Hashed `channel_id` (an internal hash, not an external string), numeric metrics, booleans, and counters stay direct. The whitelist already permits these keys; the guard adds masking/length/normalization that the direct calls bypassed.

**Alternative:** route *every* attribute through the guard. Rejected — usage/cost/counters are internal numeric values that the guard would needlessly re-normalize; the spec targets external strings.

### D3 — Streaming finalizer computes Cost (P1-1)

`_TerminalFinalizer.__init__` SHALL accept `cost_calculator` and `resolved_model`. In each terminal path (`finalize_success/error/cancelled`), after `capture_usage`/`_apply_usage_to_attempt`, compute `cost = cost_calculator.calculate(usage, model=resolved_model)` (fail-open), call `attempt.set_cost(cost)`, and include `cost` in the `AttemptResult`. `GatewayStream`/`AsyncGatewayStream` pull `cost_calculator` from the runtime handle and `resolved_model` from the attempt; `wrap_stream`/`wrap_async_stream` and `GatewayRuntimeHandle.finalize_streaming_attempt` thread them through. This mirrors `runtime.finalize_attempt`'s non-streaming cost path.

### D4 — Streaming `unknown → stream_interrupted` (P1-2)

In `_TerminalFinalizer._classify`, after `classify_error(error)` returns, if `category == ErrorCategory.UNKNOWN`, set `category = ErrorCategory.STREAM_INTERRUPTED` and `retryable = True`. This is local to the streaming finalizer; `classify_error()` and non-streaming paths are untouched. Rationale: the streaming contract's error-category set is `(stream_interrupted, timeout, connect_error, client_cancelled)`; `unknown` is not a valid streaming terminal category.

### D5 — Minimal real HTTP E2E harness (P1-3)

Two real servers, both started on ephemeral ports in the test process:

1. **Mock Core HTTP server** (`gateway_http_harness.py`): an `aiohttp.web` app exposing `POST /api/v1/ingest` that stores records (mirrors Phase 2.5's `MockCoreHandler` but async + queryable). The SDK `Reporter(endpoint=<core url>)` started via `start_sync()` POSTs real HTTP to it — no `reporter.report` monkeypatch.
2. **Gateway HTTP harness**: an `aiohttp.web` app exposing `POST /v1/chat/completions` (and a streaming variant) whose handler runs the real `GatewayRuntime`: adapter extracts request/route/attempt context → `handle_request` → `start_attempt` → forward to a **mock upstream** (deterministic: success / retryable-5xx / fallback / stream / cancel) → `finish_attempt`/`finalize_streaming_attempt` → return the upstream response.

The E2E test (`test_gateway_http_e2e.py`) drives a real `httpx`/`aiohttp` client against the gateway URL and asserts Router/Attempt records arrived at the Mock Core via real HTTP (valid TraceIDs, parent links, hashed channels, streaming consistency, empty registries). The existing live-upstream test is renamed to `test_live_upstream_runtime_e2e` (its scope) and stays secret-gated.

**Alternative:** build the harness on the existing `proxy/` aiohttp app. Rejected — the proxy uses the legacy `trace_context` path, not the new `GatewayRuntime`; wiring them would entangle two telemetry stacks. A dedicated minimal harness isolates the new runtime.

### D6 — `RLock` on the open-attempt registry (P1-4)

Add `self._open_attempts_lock = threading.RLock()` on the Router. Wrap `register_open_attempt`, `unregister_open_attempt`, the `open_attempts` snapshot, `open_attempt_count`, and the snapshot+clear in `_force_close_open_attempts` with the lock. `RLock` (not `Lock`) because `_force_close_open_attempts` calls `force_close` → `close` → `unregister_open_attempt`, which re-enters the lock. Snapshot-under-lock guarantees finalize sees a stable set; clear-under-lock guarantees no post-clear registration leaks.

## Risks / Trade-offs

- [force_close aggregating could double-report if an attempt was already finalized then force-closed] → the `_aggregated_to_router` guard already prevents re-aggregation; `test_force_closed_attempt_does_not_duplicate_report` covers it.
- [set_gateway_attribute now masks/truncates values that existing tests assert verbatim] → audit each replaced call site; the guard's whitelist already permits these keys and only adds masking of secret patterns (most values are clean provider/model names).
- [streaming cost on a cancel with partial usage] → fail-open; partial usage → priced or unpriced per the table, never an exception.
- [HTTP harness adds startup/teardown complexity to the E2E suite] → servers are ephemeral-port, daemon-threaded, and torn down per test; the suite stays fast (mock upstream, no network egress).
- [RLock on every registry op adds negligible contention] → registry ops are O(1) dict ops; the lock is uncontended except under genuine concurrency, which is exactly when it's needed.

## Migration Plan

1. Blocker 1 (force-close aggregation) + P1-4 (registry lock) together — both touch Router/Attempt finalize paths.
2. Blocker 2 (privacy wiring) — Router/Attempt attribute call sites.
3. P1-1 + P1-2 (streaming cost + classification) — `_TerminalFinalizer`.
4. P1-3 (HTTP E2E harness) — new files, no runtime change.
5. Full local regression + push; archive after CI green + 0-skipped.

## Open Questions

- Whether the gateway HTTP harness should also support the live upstream (secret-gated) in addition to the mock upstream. Decided: no — the live-upstream runtime test already covers real upstream; the harness proves the HTTP/glue layer with a deterministic mock upstream.
