# Delta: gateway-observability-runtime

本 delta 处理归档 closeout 之后的 6 个遗留缺陷（2 冻结阻塞 + 4 P1），不修改已归档历史。

## MODIFIED Requirements

### Requirement: RouterSpan and AttemptSpan lifecycle and registries

The runtime SHALL provide `RouterSpan` and `AttemptSpan` runtime objects whose spans remain open from request start until a terminal state. The runtime SHALL maintain per-request Router and Attempt registries and clean them on success, error, client cancel, generator close, async generator aclose, and span-end failure. No ContextVar, Registry entry, streaming wrapper reference, or HTTP session handle SHALL leak after any terminal path.

The Router SHALL explicitly track its open Attempts (`_open_attempts: dict[str, AttemptSpan]`): an Attempt registers on start and unregisters on close. The `_open_attempts` registry SHALL be protected by an independent re-entrant lock covering registration, unregistration, snapshot, force-close iteration, and clear, so concurrent attempts, hedged requests, or a Router-finalize racing an Attempt-close cannot miss or double an entry. Router `finalize()` SHALL force-close every remaining open Attempt via an idempotent, fail-open `force_close(category="gateway_internal", reason="router_finalized_with_open_attempt")`. Merely deleting registry entries without ending the spans is forbidden. `force_close()` SHALL NOT overwrite an already-recorded business error, SHALL clean the registry and context entries, and SHALL report the final span. After Router finalize, the open-attempt registry SHALL be empty and no attempt SHALL be reported twice.

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

#### Scenario: Open-attempt registry is concurrency-safe

- **WHEN** Attempts start, close, and force-close concurrently, or a Router finalize races an Attempt close
- **THEN** no registry entry is missed or double-counted
- **AND** the finalize snapshot is stable (an attempt that closed before finalize is not force-closed; one still open is)

### Requirement: PrivacyGuard

The runtime SHALL provide a `PrivacyGuard` that blocks by default the recording of Authorization headers, API Keys, Cookies, Set-Cookie, raw channel secrets, full upstream URLs, full prompts, full responses, tool inputs, tool outputs, and uploaded files. It SHALL allow provider names, hashed channel IDs, model names, HTTP status, Usage, Cost, error categories, and request IDs. Channel IDs SHALL be hashed/HMAC'd from internal raw IDs by default, and the system SHALL NOT persist secret names or tokens.

All Router and Attempt span attributes carrying external strings (including `route`, `route_reason`, `policy_name`, `request_id`, `provider`, `resolved_model`, `requested_model`, `upstream_request_id`, `error_type`, `error_message`, and `finish_reason`) SHALL be written exclusively through a single guarded entry point `set_gateway_attribute(span, key, value, privacy_guard)`. The guard SHALL apply: a field-name whitelist (unknown keys are denied by default), value sanitization, length limits (single strings at most 512 bytes; `request_id` at most 256 bytes; `route` at most 256 bytes; `reason` at most 256 bytes; provider/model at most 128 bytes), type normalization, and a size guard, and SHALL be fail-open. Internal counters, booleans, hashed channel IDs, and numeric metrics MAY be written directly. Router and Attempt code SHALL NOT write unguarded external strings directly.

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

#### Scenario: Runtime external strings guarded through the real path

- **WHEN** a `GatewayRuntime` request carries a route with a query secret, an oversized request_id, a route_reason bearing a secret, an upstream_request_id, or an error_message bearing a secret
- **THEN** the Router/Attempt span attributes recorded by the real runtime path have the query stripped, the value truncated to its limit, and the secret redacted
- **AND** an unknown external attribute key is not recorded on a real Router/Attempt span

### Requirement: Streaming wrapper lifecycle

The runtime SHALL provide a streaming wrapper that keeps Router and Attempt spans open across a stream and ends them only at a terminal state (full consumption, `[DONE]`, client disconnect/cancel, upstream timeout, upstream connection error, generator `close`, async generator `aclose`). The runtime SHALL split finalization into `finish_non_streaming_attempt(...)` and `finalize_streaming_attempt(...)`; the streaming path SHALL NOT finish the Attempt or aggregate a success result to the Router at response-header receipt or wrapper creation. At the terminal state the wrapper SHALL construct the final `AttemptResult`, aggregate it to the Router exactly once, close the Attempt, and close the Router, with Router and Attempt terminal states consistent per the contract. It SHALL record TTFT exactly once (from the real upstream request start, ignoring keepalives, empty chunks, metadata-only or usage-only chunks, and `[DONE]`) and the corresponding `gateway.stream.*` events. On any terminal path, including failures during stream finalization, it SHALL clean up ContextVars, registries, wrapper references, background tasks, and HTTP session handles. Stream close SHALL be idempotent: repeated `close()`/`aclose()` or cancellation after finalization SHALL NOT re-aggregate or re-report.

At the terminal state the streaming finalizer SHALL compute the Attempt Cost from the captured terminal Usage using the `CostCalculator` and the Attempt's `resolved_model` (fail-open), write it to the Attempt, include it in the `AttemptResult`, and aggregate it into the Router cost aggregate — so streaming attempts carry `cost.*` exactly like non-streaming attempts. For a stream error or cancel, when the classifier returns `unknown`, the streaming finalizer SHALL remap the category to `stream_interrupted`; this remap is local to the streaming funnel and SHALL NOT alter global `classify_error()` behavior.

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

#### Scenario: Streaming terminal Usage produces Cost

- **WHEN** a terminal stream chunk carries Usage and the resolved model has a pricing-table entry
- **THEN** the Attempt carries `cost.*` computed from that Usage and the resolved model
- **AND** the Router cost aggregate includes the streaming attempt's cost
- **AND** when the resolved model has no pricing entry, `cost.source = unpriced` and no exception propagates

#### Scenario: Unclassified streaming interruption is stream_interrupted

- **WHEN** a stream fails mid-consumption with an exception the global classifier cannot recognize (e.g. a parse or protocol error)
- **THEN** the Attempt `gateway.error_category` is `stream_interrupted` (not `unknown`)
- **AND** the Router `gateway.final_error_category` equals `stream_interrupted`

### Requirement: UsageNormalizer and CostCalculator

The runtime SHALL provide a `UsageNormalizer` that maps provider-specific usage payloads (OpenAI chat completion, Anthropic Messages, and OpenAI-compatible responses) into `NormalizedUsage` (`input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`, `reasoning_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `usage_source`) and a `CostCalculator` that maps normalized usage to `NormalizedCost` (`input_cost`, `output_cost`, `total_cost`, `currency`, `cost_source`). Normalization and cost-calc failures SHALL be fail-open: the span still ends with whatever data was successfully recorded.

Attempt cost SHALL be calculated with the Attempt's resolved model: `calculate(usage=normalized, model=attempt.resolved_model)`. The pricing table SHALL use explicit per-1M-token units, configured under unambiguous names `input_usd_per_1m_tokens` and `output_usd_per_1m_tokens`. When no price exists for the resolved model, `cost.source` SHALL be `unpriced`. For cache hits, an explicitly caller-provided cost SHALL be preserved; when no cost is provided but usage exists, cost SHALL be computed from the resolved model. Router cost SHALL include the cost of failed and retried attempts. Streaming attempts SHALL compute Cost from their captured terminal Usage by the same `CostCalculator.calculate(usage, model=resolved_model)` path as non-streaming attempts.

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

### Requirement: Real gateway E2E and CI gates

The system SHALL provide a real gateway E2E suite exercising the full chain — real HTTP client → gateway middleware/adapter → GatewayRuntime → Router → Attempt → mock or real upstream → Reporter → Core ingest API — rather than only in-memory runtime object checks or a runtime driving `runtime.handle_request(...)` directly with a monkeypatched reporter. A minimal real HTTP gateway harness (ASGI/aiohttp) SHALL wrap the `GatewayRuntime` behind a real `POST /v1/chat/completions` endpoint, and a real Mock Core HTTP server SHALL receive records via the SDK Reporter's real HTTP `POST /api/v1/ingest` (no `reporter.report` monkeypatch). The suite SHALL cover at minimum: one success, one retry, one fallback, streaming success, streaming cancel, a no-SDK trace, an upstream `sampled=0` request, and privacy. Hard assertions SHALL include: Router/Attempt records actually ingested by Core over HTTP; valid TraceIDs; Attempt parent = Router; Router parent = SDK LLM or remote parent; retries produce multiple unique Attempts; fallback from/to present and hashed; streaming terminal-state consistency; and empty registries/contexts at the end. A separate, secret-gated live-upstream test MAY verify the runtime against a real provider; it is not a substitute for the HTTP harness. The suite SHALL NOT reuse the Phase 2.5 real-E2E test file.

CI SHALL use the unified variables `GATEWAY_E2E_API_KEY`, `GATEWAY_E2E_BASE_URL`, and `GATEWAY_E2E_MODEL`, read under those exact names by the live-upstream test. On trusted branches, a missing secret SHALL fail the live-upstream job; on fork PRs the entire secret job SHALL be skipped. Log redaction SHALL use `scripts/redact_ci_secrets.py` (not a single-quoted `sed` expression embedding the secret). CI SHALL assert the run executed more than zero tests and that the number of skipped required-E2E tests is zero. The deterministic HTTP-harness E2E (mock upstream) SHALL run in the always-on gateway test job, not the secret-gated one.

#### Scenario: Real E2E ingests Router and Attempt into Core over HTTP

- **WHEN** the HTTP-harness E2E suite runs a success, a retry, a fallback, a streaming success, and a streaming cancel
- **THEN** Core ingest (reached via real Reporter HTTP) contains the Router and all Attempt records with valid TraceIDs and correct parent links
- **AND** retry produces multiple unique Attempt spans
- **AND** fallback events carry hashed from/to channel IDs
- **AND** registries and contexts are empty after each scenario

#### Scenario: Trusted branch fails when secrets are missing

- **WHEN** the live-upstream CI job runs on a trusted branch without the gateway E2E secrets
- **THEN** the job fails rather than passing with all tests skipped
- **AND** on a fork PR the secret job does not run at all

#### Scenario: CI rejects silent skips

- **WHEN** the gateway E2E job completes
- **THEN** the executed test count is greater than zero
- **AND** the count of skipped required-E2E tests is zero

## ADDED Requirements

### Requirement: Force-closed attempt aggregates failure to the Router

When `Router.finalize()` force-closes an open Attempt, the force-close SHALL construct the terminal `AttemptResult` (with `success = False`, the `gateway_internal` error, and the Attempt's captured Usage and Cost if any) and aggregate it into the Router exactly once (idempotent via the attempt's `_aggregated_to_router` guard). Consequently the Router's `fail_count` SHALL increment, its `final_error_category` SHALL be `gateway_internal` (unless a business error on a later/other attempt supersedes it), and the Router SHALL end `ERROR` with exactly one `gateway.response.failed` event — never `OK` while a child Attempt is `ERROR`. A force-closed Attempt that already recorded a business error SHALL aggregate that business error (not `gateway_internal`) and SHALL NOT be re-aggregated.

#### Scenario: Force-closed attempt makes the Router ERROR

- **WHEN** a Router finalizes with one open Attempt and no prior successful attempt
- **THEN** the Router span ends with status `ERROR`
- **AND** `gateway.final_error_category` is `gateway_internal`

#### Scenario: Force-closed attempt records response_failed exactly once

- **WHEN** a Router finalizes with an open Attempt
- **THEN** exactly one `gateway.response.failed` event is recorded on the Router before it ends
- **AND** no `gateway.response.completed` event is recorded

#### Scenario: Multiple force-closed attempts increment fail_count

- **WHEN** a Router finalizes with N open Attempts and no prior successful attempt
- **THEN** the Router `fail_count` is N
- **AND** each open Attempt is reported exactly once

#### Scenario: Force-closed attempt with a business error preserves it

- **WHEN** an open Attempt already recorded a business error (e.g. `timeout`) and is then force-closed at Router finalize
- **THEN** the aggregated `final_error_category` is the business error category (not `gateway_internal`)
- **AND** the Attempt is not re-aggregated if it was already finalized
