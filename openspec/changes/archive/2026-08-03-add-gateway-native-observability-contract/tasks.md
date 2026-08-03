## 1. Gateway Contract & Data Model

- [x] 1.1 Create `sdk/python/llm_observability/gateway_observability/` package skeleton with `__init__.py` exporting the public API.
- [x] 1.2 Implement `attributes.py` with the frozen attribute-name constants (`gateway.*`, `usage.*`, `cost.*`) and `gateway.span_role` constants `router` / `provider_attempt`.
- [x] 1.3 Implement `events.py` with the fixed gateway event-name constants and the allowed event-attribute whitelist (`reason`, `attempt_index`, `channel_id`, `provider`, `resolved_model`, `delay_ms`, `error_category`, `http_status_code`).
- [x] 1.4 Implement data models in `context.py` (`GatewayRequestContext`), `router_span.py`/`runtime.py` (`RouteDecision`), `attempt_span.py`/`runtime.py` (`AttemptContext`), `usage.py` (`NormalizedUsage`), `cost.py` (`NormalizedCost`) as dataclasses matching spec §8.
- [x] 1.5 Implement `errors.py` with the fixed error taxonomy constants (authentication, authorization, rate_limit, quota, timeout, connect_error, dns_error, tls_error, provider_4xx, provider_5xx, invalid_request, invalid_response, stream_interrupted, client_cancelled, gateway_internal, unknown).

## 2. Gateway Runtime (Router / Attempt)

- [x] 2.1 Implement `context.py` runtime context: `GatewayContext` ContextVar holding `(router, active_attempt)` with `get/set/reset` and fail-open reset.
- [x] 2.2 Implement Router and Attempt registries with cleanup on success, error, cancel, close, aclose, and span-end failure.
- [x] 2.3 Implement `router_span.py` `RouterSpan` context manager: creates Router GATEWAY span with role attribute, parent = SDK LLM span when present else Root, duration metrics, retry/fallback/attempt counts, final channel, final status/error.
- [x] 2.4 Implement `attempt_span.py` `AttemptSpan` context manager: parent = Router span, `gateway.attempt_index`, upstream status/duration/ttft, error_type/category/message, finish_reason, retryable.
- [x] 2.5 Implement `runtime.py` `GatewayRuntime` orchestrator tying adapter → RouterSpan → AttemptSpan → recorder → reporter, with each step isolated in try/except (fail-open).
- [x] 2.6 Implement `GatewayEventRecorder` recording fixed events with the whitelisted attribute set; event add failure is fail-open.

## 3. Retry / Fallback / Cache / Rate-limit

- [x] 3.1 Implement `retry_scheduled(attempt_index, delay_ms, reason)` event recording with `gateway.retry.scheduled`; each retry creates a fresh Attempt span (never reuse).
- [x] 3.2 Implement `fallback_selected(from_channel_id, to_channel_id, reason)` recording a single `gateway.fallback.selected` event with from/to channels differing.
- [x] 3.3 Implement cache handling: `cache_status=hit|miss|bypass|error`, hit → Router exists with `attempt_count=0` and no Attempt created.
- [x] 3.4 Implement rate-limit rejection: Router status ERROR, `error_category=rate_limit`, `attempt_count=0`, no fake Attempt.

## 4. Streaming Lifecycle

- [x] 4.1 Implement `streaming.py` sync wrapper that keeps Router/Attempt spans open until full consumption / `[DONE]` / client disconnect / close / upstream error; TTFT recorded once; `gateway.stream.*` events.
- [x] 4.2 Implement `streaming.py` async wrapper (same terminal states plus `aclose` and `GeneratorExit`); both spans end only at terminal state, never at response-header/first-token/`StreamingResponse` return.
- [x] 4.3 Implement client-cancel handling: `gateway.stream.cancelled`, `error_category=client_cancelled`, both spans ended, all ContextVars/registries/wrapper/background-task/session handles cleaned (fail-open on finalization failure).
- [x] 4.4 Add streaming wrapper tests covering `close()`/`aclose()`/`break`/`CancelledError` without residual registry/ContextVar state.

## 5. Usage / Cost

- [x] 5.1 Implement `usage.py` `UsageNormalizer` mapping OpenAI/OpenAI-compatible usage payloads (`prompt_tokens`/`completion_tokens`/`total_tokens`, cached/reasoning variants when present) into `NormalizedUsage` with `usage_source`; parse failure is fail-open.
- [x] 5.2 Implement `cost.py` `CostCalculator` mapping `NormalizedUsage` → `NormalizedCost` with `currency="USD"` and `cost.source="unpriced"` when no pricing table; calc failure is fail-open.
- [x] 5.3 Implement Usage/Cost aggregation at Attempt (per-request incl. failed attempts with Provider-returned usage) and Router (aggregate of all attempts incl. failures, success/fail counts, final channel).
- [x] 5.4 Implement SDK LLM usage aggregation hook: when a Router exists, LLM final Usage equals Router aggregate (never only the final successful attempt); without a Router, LLM keeps its own usage.

## 6. Privacy / Sampling / Fail-open

- [x] 6.1 Implement `privacy.py` `PrivacyGuard`: default-deny secrets (Authorization, API Key, Cookie, Set-Cookie, raw channel secret, full URL query, full prompt/response, tool I/O, uploads); allow provider/model/status/usage/cost/error category/request IDs; channel ID hashing/HMAC; fail-closed masking (`<redacted>`).
- [x] 6.2 Implement sampling inheritance in `propagation.py`: honor upstream `traceparent` `trace_flags` (01 sample / 00 no report), never re-randomize; no-upstream → local `sample_rate` Root Router; sampled-out still runs business + propagates traceparent + no Reporter Record + no large serialization.
- [x] 6.3 Implement fail-open coverage across span create/end, set_attribute, add_event, usage parse, cost calc, reporter, context reset, and stream finalization; every swallowed step logs at ERROR.
- [x] 6.4 Add fault-injection tests asserting business primacy AND that each telemetry failure is logged.

## 7. GatewayAdapter & One-API Adapter

- [x] 7.1 Implement `adapter.py` `GatewayAdapter` ABC (`extract_request_context`, `extract_route_decision`, `extract_attempt_context`, `extract_usage`, `classify_error`) plus a `GenericAdapter` reference implementation.
- [x] 7.2 Document the minimal `internal_state` contract that `extract_route_decision`/`extract_attempt_context` accept per gateway.
- [x] 7.3 Create `integrations/oneapi/` package with `adapter.py`, `request_mapper.py`, `response_mapper.py`, `channel_mapper.py`, `retry_mapper.py`, `usage_mapper.py` mapping One-API request token/channel/model mapping/relay mode/retry/fallback/quota/upstream response to the contract.
- [x] 7.4 Ensure One-API adapter obeys boundaries: no channel-selection/retry/timeout/quota mutation, no business-exception swallowing, no persistence/trace-id generation/HTTP reporting.
- [x] 7.5 Create reserved `integrations/litellm/adapter.py` interface stub implementing `GatewayAdapter` for future LiteLLM support.

## 8. Tests

- [x] 8.1 Create `tests/gateway_observability/` suite: `test_router_span.py`, `test_attempt_span.py`, `test_retry.py`, `test_fallback.py`, `test_streaming.py`, `test_usage.py`, `test_cost.py`, `test_privacy.py`, `test_sampling.py`, `test_fail_open.py`, `test_registry_cleanup.py`, `test_oneapi_adapter.py`.
- [x] 8.2 Implement mock gateway harness E2E: Scenario A success, Scenario B retry (500→200), Scenario C fallback (timeout→success), Scenario D cache hit, Scenario E rate limit, Scenario F streaming success, Scenario G streaming cancel, Scenario H no-SDK root, Scenario I sampling=0, Scenario J privacy (no secrets in any span/event/log).
- [x] 8.3 Run full Phase 2.1–2.5 regression suite and keep it green (existing proxy flat GATEWAY path untouched).

## 9. CI

- [x] 9.1 Add CI jobs `gateway-runtime-tests`, `oneapi-adapter-tests`, `gateway-streaming-tests`.
- [x] 9.1 Add CI jobs `gateway-runtime-tests`, `oneapi-adapter-tests`, `gateway-streaming-tests`.
- [x] 9.2 Add `gateway-real-e2e` job: trusted-branch-only, secret-gated (missing secret fails), Fork PRs skip secret job, logs redacted. **NOTE (2026-08-03): deferred to Phase 3.1** — round one validates the gateway runtime via the OFFLINE Scenario A–J mock harness in `oneapi-adapter-tests`; a real One-API gateway E2E (needs a One-API instance + One-API token, not a single-vendor model key) is added when the One-API adapter is wired into the gateway server (design D12, spec §23/§30).
- [x] 9.3 Add `phase2-regression` job to the change's CI wiring.
