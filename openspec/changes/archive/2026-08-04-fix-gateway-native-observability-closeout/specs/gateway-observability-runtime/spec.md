# Delta: gateway-observability-runtime

本 delta 依据 `docs/llm-observability-phase3-rework-bug-fix-requirements.md` 的 P0-1/P0-3/P0-4/P0-5/P0-6/P1-1/P1-2/P1-3/P1-5/P1-6 冻结修正后的运行时语义。

## MODIFIED Requirements

### Requirement: RouterSpan and AttemptSpan lifecycle and registries

The runtime SHALL provide `RouterSpan` and `AttemptSpan` runtime objects whose spans remain open from request start until a terminal state. The runtime SHALL maintain per-request Router and Attempt registries and clean them on success, error, client cancel, generator close, async generator aclose, and span-end failure. No ContextVar, Registry entry, streaming wrapper reference, or HTTP session handle SHALL leak after any terminal path.

The Router SHALL explicitly track its open Attempts (e.g., `_open_attempts: dict[str, AttemptSpan]`): an Attempt registers on start and unregisters on close. Router `finalize()` SHALL force-close every remaining open Attempt via an idempotent, fail-open `force_close(category="gateway_internal", reason="router_finalized_with_open_attempt")`. Merely deleting registry entries without ending the spans is forbidden. `force_close()` SHALL NOT overwrite an already-recorded business error, SHALL clean the registry and context entries, and SHALL report the final span. After Router finalize, the open-attempt registry SHALL be empty and no attempt SHALL be reported twice.

#### Scenario: Normal completion cleans registries

- **WHEN** a routed request completes normally
- **THEN** the Router and Attempt registry entries, ContextVars, and streaming wrapper references are all cleared

#### Scenario: Registry cleanup after client cancel

- **WHEN** a client cancels mid-stream
- **THEN** the Router and Attempt registries are empty afterward
- **AND** no ContextVar for the active gateway context remains

#### Scenario: Span end failure still cleans up

- **WHEN** `span.end()` raises during finalization
- **THEN** registries and ContextVars are still cleaned and the failure does not leak into business behavior

#### Scenario: Router finalize force-closes open attempts

- **WHEN** a business exception occurs between Attempt start and close, and the Router is then finalized
- **THEN** every open Attempt is force-closed with `gateway_internal` / `router_finalized_with_open_attempt`
- **AND** the open-attempt registry is empty
- **AND** each force-closed Attempt's final span is reported exactly once

#### Scenario: force_close is idempotent and preserves business errors

- **WHEN** `force_close()` is called twice on the same Attempt, or on an Attempt that already recorded a business error
- **THEN** the second call is a no-op
- **AND** the previously recorded business error is not overwritten
- **AND** no duplicate report is emitted after a normally ended Attempt

### Requirement: GatewayEventRecorder

The runtime SHALL provide a `GatewayEventRecorder` that records the fixed gateway events with the limited attribute set from the contract. A recorder failure SHALL be fail-open and SHALL NOT alter the span state or business outcome. The recorder SHALL be wired into the actual runtime lifecycle: `gateway.route.selected` on route selection, `gateway.attempt.started` on Attempt start, `gateway.attempt.completed` on Attempt success, `gateway.attempt.failed` on Attempt failure, and `gateway.response.completed` / `gateway.response.failed` at Router terminal states. Each terminal event SHALL be recorded at most once and SHALL be written before the span ends; event attributes SHALL pass through the PrivacyGuard. An Attempt SHALL never carry both a completed and a failed event.

#### Scenario: Event add failure is fail-open

- **WHEN** adding a gateway event raises
- **THEN** the span and business flow continue unchanged

#### Scenario: Lifecycle events recorded exactly once

- **WHEN** an Attempt starts, then succeeds or fails
- **THEN** exactly one `gateway.attempt.started` event exists
- **AND** exactly one terminal event (`gateway.attempt.completed` or `gateway.attempt.failed`, never both) exists, written before span end

#### Scenario: Router terminal event exactly once

- **WHEN** a Router reaches a success or failure terminal state
- **THEN** exactly one of `gateway.response.completed` / `gateway.response.failed` is recorded before the Router span ends

### Requirement: UsageNormalizer and CostCalculator

The runtime SHALL provide a `UsageNormalizer` that maps provider-specific usage payloads (OpenAI chat completion, Anthropic Messages, and OpenAI-compatible responses) into `NormalizedUsage` (`input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`, `reasoning_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `usage_source`) and a `CostCalculator` that maps normalized usage to `NormalizedCost` (`input_cost`, `output_cost`, `total_cost`, `currency`, `cost_source`). Normalization and cost-calc failures SHALL be fail-open: the span still ends with whatever data was successfully recorded.

Attempt cost SHALL be calculated with the Attempt's resolved model: `calculate(usage=normalized, model=attempt.resolved_model)`. The pricing table SHALL use explicit per-1M-token units, configured under unambiguous names `input_usd_per_1m_tokens` and `output_usd_per_1m_tokens`. When no price exists for the resolved model, `cost.source` SHALL be `unpriced`. For cache hits, an explicitly caller-provided cost SHALL be preserved; when no cost is provided but usage exists, cost SHALL be computed from the resolved model. Router cost SHALL include the cost of failed and retried attempts.

#### Scenario: OpenAI-compatible usage normalized

- **WHEN** an OpenAI-compatible response contains `prompt_tokens`, `completion_tokens`, and `total_tokens`
- **THEN** the normalizer yields `input_tokens`, `output_tokens`, and `total_tokens` with a populated `usage_source`

#### Scenario: Usage parse failure is fail-open

- **WHEN** a usage payload cannot be parsed
- **THEN** no exception propagates and the span ends with the successfully recorded attributes

#### Scenario: Cost calculation failure is fail-open

- **WHEN** the cost calculator raises
- **THEN** no exception propagates and the cost attributes remain unset or partially set

#### Scenario: Cost uses the resolved model

- **WHEN** an Attempt resolves to a model present in the pricing table
- **THEN** the Attempt cost is computed with that resolved model's per-1M-token prices
- **AND** the pricing configuration names are `input_usd_per_1m_tokens` / `output_usd_per_1m_tokens`

#### Scenario: Unknown model is unpriced

- **WHEN** the resolved model has no pricing entry
- **THEN** `cost.source = unpriced`

#### Scenario: Cache explicit cost preserved; retry costs summed

- **WHEN** a cache hit supplies an explicit cost
- **THEN** that cost is used instead of recomputation
- **AND** when retries occur, the Router cost sums all attempts including failed ones

### Requirement: PrivacyGuard

The runtime SHALL provide a `PrivacyGuard` that blocks by default the recording of Authorization headers, API Keys, Cookies, Set-Cookie, raw channel secrets, full upstream URLs, full prompts, full responses, tool inputs, tool outputs, and uploaded files. It SHALL allow provider names, hashed channel IDs, model names, HTTP status, Usage, Cost, error categories, and request IDs. Channel IDs SHALL be hashed/HMAC'd from internal raw IDs by default, and the system SHALL NOT persist secret names or tokens.

All Router and Attempt span attributes carrying external strings (including `route`, `route_reason`, `policy_name`, `request_id`, `provider`, `resolved_model`, `upstream_request_id`, and `error_message`) SHALL be written exclusively through a single guarded entry point `set_gateway_attribute(span, key, value, privacy_guard)`. The guard SHALL apply: a field-name whitelist (unknown keys are denied by default), value sanitization, length limits (single strings at most 512 bytes; `request_id` at most 256 bytes; `route` at most 256 bytes; `reason` at most 256 bytes; provider/model at most 128 bytes), type normalization, and a size guard, and SHALL be fail-open. Router and Attempt code SHALL NOT write unguarded external strings directly.

#### Scenario: Channel ID hashed

- **WHEN** a gateway records a channel ID
- **THEN** the telemetry value is the hash/HMAC of the internal raw ID, not the raw ID

#### Scenario: Secret attributes blocked

- **WHEN** telemetry emission encounters an Authorization header or API Key value
- **THEN** the value is not recorded in any span, event, or log

#### Scenario: External values sanitized and size-limited

- **WHEN** a route contains a query string, an error message contains a secret, or a request_id exceeds 256 bytes
- **THEN** the recorded attribute has the query removed, the secret redacted, and the value truncated to its limit

#### Scenario: Unknown attribute keys denied by default

- **WHEN** code attempts to set a span attribute whose key is not whitelisted
- **THEN** the attribute is not recorded
- **AND** no exception propagates to business flow

### Requirement: Gateway context and propagation

The runtime SHALL manage an active gateway context via ContextVars with separate Router and Attempt slots (e.g., `GatewayContextState{router, active_attempt}`), and SHALL restore the parent context on success, error, GeneratorExit, and async generator `aclose`. Closing an Attempt — normally, on error, in async paths, or via a cross-context reset — SHALL clear only the Attempt slot and SHALL NOT clear or reset the Router slot; calling a wholesale `clear_gateway_context()` on Attempt close is forbidden. Only a Router terminal state may clear the Router slot, and Router close SHALL clear both slots. The runtime SHALL accept a valid W3C `traceparent` from an upstream SDK trace and use its TraceID and sampling decision; with no upstream trace it SHALL create a Root Router. Context reset failures SHALL be fail-open.

The runtime SHALL provide `inject_downstream_trace_headers(router, attempt)` so each Attempt propagates a downstream `traceparent` whose trace ID equals the Router's trace ID, whose parent span ID equals the Attempt's span ID, and whose `trace_flags` carry the inherited sampling decision (`00` when sampled out, `01` when sampled). A sampled-out decision SHALL still propagate the header downstream.

#### Scenario: Context restored on error

- **WHEN** a router/attempt context exits with an error
- **THEN** the parent context is restored and no stale gateway ContextVar remains

#### Scenario: Context reset failure is fail-open

- **WHEN** context reset raises during exit
- **THEN** the business exception or result is preserved

#### Scenario: Attempt close preserves the active router

- **WHEN** an Attempt closes normally, on error, in an async path, or via cross-context reset
- **THEN** the Router slot remains active (`runtime.active_router()` is unchanged)
- **AND** only the Attempt slot is cleared
- **AND** a subsequent retry or fallback Attempt uses the same Router

#### Scenario: Router close clears both slots

- **WHEN** the Router reaches its terminal state
- **THEN** both the Router and Attempt slots are cleared

#### Scenario: Downstream traceparent injected from the attempt

- **WHEN** an Attempt issues its upstream request
- **THEN** the downstream `traceparent` has the Router's trace ID, the Attempt's span ID as parent, and the inherited sampled flag
- **AND** a sampled-out decision propagates `trace_flags=00` while a sampled decision propagates `trace_flags=01`

### Requirement: Streaming wrapper lifecycle

The runtime SHALL provide a streaming wrapper that keeps Router and Attempt spans open across a stream and ends them only at a terminal state (full consumption, `[DONE]`, client disconnect/cancel, upstream timeout, upstream connection error, generator `close`, async generator `aclose`). The runtime SHALL split finalization into `finish_non_streaming_attempt(...)` and `finalize_streaming_attempt(...)`; the streaming path SHALL NOT finish the Attempt or aggregate a success result to the Router at response-header receipt or wrapper creation. At the terminal state the wrapper SHALL construct the final `AttemptResult`, aggregate it to the Router exactly once, close the Attempt, and close the Router, with Router and Attempt terminal states consistent per the contract. It SHALL record TTFT exactly once (from the real upstream request start, ignoring keepalives, empty chunks, metadata-only or usage-only chunks, and `[DONE]`) and the corresponding `gateway.stream.*` events. On any terminal path, including failures during stream finalization, it SHALL clean up ContextVars, registries, wrapper references, background tasks, and HTTP session handles. Stream close SHALL be idempotent: repeated `close()`/`aclose()` or cancellation after finalization SHALL NOT re-aggregate or re-report.

#### Scenario: Streaming wrapper tracks full consumption

- **WHEN** a stream is consumed to completion
- **THEN** the wrapper ends both spans with status OK after the final chunk and records `gateway.stream.completed`
- **AND** the Attempt result is registered with the Router exactly once

#### Scenario: Streaming wrapper handles early close

- **WHEN** a consumer closes the generator or async generator before completion
- **THEN** the wrapper ends both spans with consistent cancel semantics, records `gateway.stream.cancelled`, and cleans all registry and ContextVar state
- **AND** repeated close/aclose calls finalize only once

#### Scenario: No success aggregation before terminal state

- **WHEN** response headers have been received and the wrapper created, but the stream has not terminated
- **THEN** no successful `AttemptResult` has been aggregated to the Router
- **AND** neither span has ended

#### Scenario: Stream finalization failure is fail-open

- **WHEN** wrapper finalization raises
- **THEN** the consumer-visible stream behavior is unchanged

## ADDED Requirements

### Requirement: Attempt index allocation

The Router SHALL allocate default Attempt indices via `router.allocate_attempt_index()`, incrementing per Attempt and safe under concurrency. An explicitly provided valid positive integer index SHALL be used as given; a duplicate explicit index SHALL be remapped to the next available value with a warning recorded; zero, negative, or non-integer values SHALL fall back to automatic allocation. The Router's `gateway.attempt_count` SHALL equal the actual number of Attempts.

#### Scenario: Default indices increment

- **WHEN** two Attempts start without explicit indices
- **THEN** their indices are distinct and increasing
- **AND** `gateway.attempt_count` equals the real number of Attempts

#### Scenario: Duplicate or invalid explicit index handled

- **WHEN** an explicit duplicate, zero, negative, or non-integer index is provided
- **THEN** duplicates are remapped to the next available value with a warning
- **AND** invalid values fall back to automatic allocation

#### Scenario: Concurrent allocation is unique

- **WHEN** Attempts start concurrently without explicit indices
- **THEN** no two Attempts share an index

### Requirement: Real gateway E2E and CI gates

The system SHALL provide a real gateway E2E suite (`sdk/tests/gateway_observability/test_real_gateway_e2e.py`) exercising the full chain — client → gateway middleware/adapter → GatewayRuntime → Router → Attempt → mock or real upstream → Reporter → mock Core ingest — rather than only in-memory runtime object checks. It SHALL cover at minimum: one success, one retry, one fallback, streaming success, streaming cancel, a no-SDK trace, an upstream `sampled=0` request, and privacy. Hard assertions SHALL include: Router/Attempt records actually ingested by Core; valid TraceIDs; Attempt parent = Router; Router parent = SDK LLM or remote parent; retries produce multiple unique Attempts; fallback from/to present and hashed; streaming terminal-state consistency; and empty registries/contexts at the end. The suite SHALL NOT reuse the Phase 2.5 real-E2E test file.

CI SHALL use the unified variables `GATEWAY_E2E_API_KEY`, `GATEWAY_E2E_BASE_URL`, and `GATEWAY_E2E_MODEL`, read under those exact names by the tests. On trusted branches, a missing secret SHALL fail the job; on fork PRs the entire secret job SHALL be skipped. Log redaction SHALL use `scripts/redact_ci_secrets.py` (not a single-quoted `sed` expression embedding the secret). CI SHALL assert the run executed more than zero tests and that the number of skipped required-E2E tests is zero.

#### Scenario: Real E2E ingests Router and Attempt into Core

- **WHEN** the real E2E suite runs a success, a retry, a fallback, a streaming success, and a streaming cancel
- **THEN** Core ingest contains the Router and all Attempt records with valid TraceIDs and correct parent links
- **AND** retry produces multiple unique Attempt spans
- **AND** fallback events carry hashed from/to channel IDs
- **AND** registries and contexts are empty after each scenario

#### Scenario: Trusted branch fails when secrets are missing

- **WHEN** the CI job runs on a trusted branch without the gateway E2E secrets
- **THEN** the job fails rather than passing with all tests skipped
- **AND** on a fork PR the secret job does not run at all

#### Scenario: CI rejects silent skips

- **WHEN** the gateway E2E job completes
- **THEN** the executed test count is greater than zero
- **AND** the count of skipped required-E2E tests is zero
