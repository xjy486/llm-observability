## Why

Phase 2.5 is frozen (commit `63c148a`), so the SDK can already emit `GATEWAY` spans for the fact that a model request passed through a gateway — but it cannot explain what the gateway decided internally (which channel was chosen, why, whether retries/fallbacks happened, what each upstream attempt cost, or when a stream actually ended). Phase 3 introduces a gateway-decoupled **Gateway Native Observability Contract** so a single trace can answer "where did this logical call actually go, why, what failed, and how was Usage/Cost composed" — both for SDK-originated traces and for direct gateway calls with no SDK upstream.

## What Changes

- **Gateway Span role model**: keep SpanKind at the existing `AGENT/TASK/TOOL/LLM/GATEWAY` set (no new `ROUTER`/`PROVIDER` kinds); distinguish roles via `gateway.span_role = router | provider_attempt`. Router GATEWAY is a child of the SDK LLM span; each real upstream request gets one unique Attempt GATEWAY span child of the Router. **BREAKING** for GATEWAY consumers: GATEWAY spans now carry role attributes and a Router→Attempt hierarchy.
- **Gateway contract data model**: `GatewayRequestContext`, `RouteDecision`, `AttemptContext`, `NormalizedUsage`, `NormalizedCost` dataclasses plus the fixed `gateway.*` / `usage.*` / `cost.*` attribute namespaces.
- **Gateway runtime**: `RouterSpan`, `AttemptSpan`, a `GatewayEventRecorder`, context, registries, and a streaming lifecycle wrapper that keeps Router/Attempt spans open until a real terminal state (full consumption, `[DONE]`, client cancel/close, upstream error, or generator `close`/`aclose`) — never at response-header/connect/first-token/`StreamingResponse` return.
- **Retry/Fallback/Cache/Rate-limit decision recording**: each retry creates a fresh Attempt span; fallback records `from_channel_id → to_channel_id` with reason via events; cache hit creates no Attempt (`gateway.attempt_count = 0`); rate-limit rejection marks Router ERROR with `error_category=rate_limit` and zero attempts.
- **Usage/Cost ownership at three layers**: Attempt (per-request), Router (sum over all attempts including failed ones), SDK LLM (equals Router aggregate when a Router exists — never just the final successful attempt).
- **Error classification**: fixed error category taxonomy (auth/rate_limit/timeout/connect/dns/tls/provider_4xx/provider_5xx/stream_interrupted/client_cancelled/gateway_internal/unknown) with safe-string, length-limited, fail-closed error messages.
- **Privacy guard**: secrets (Authorization, API Key, Cookie, Provider secret, full URL query, full prompt/response) never enter telemetry by default; Channel IDs are hashed/HMAC'd.
- **Sampling inheritance**: a legitimate upstream `traceparent` decides sampling (`trace_flags=01` → sample, `00` → don't report) and is never overridden by gateway re-sampling; with no upstream trace the gateway may create a Root Router per local `sample_rate`; sampled-out still propagates traceparent and runs business normally.
- **Fail-open**: every telemetry step (span create/end, set_attribute, add_event, usage parse, cost calc, reporter, context reset, stream finalization) is isolated so telemetry failure never changes gateway business behavior.
- **GatewayAdapter abstraction**: `GatewayAdapter` ABC (`extract_request_context`, `extract_route_decision`, `extract_attempt_context`, `extract_usage`, `classify_error`) with a GenericAdapter, a One-API glue-layer adapter, and a replaceable LiteLLM adapter interface — adapters never persist, generate trace IDs, report HTTP, mutate routing/retry, or swallow business exceptions.
- **First-round delivery scope**: contract + runtime skeleton + Generic adapter + Success/Retry mock E2E + Privacy/Sampling/Fail-open unit tests; One-API full adapter, UI, pricing tables are deferred to Phase 3.1.
- **No SDK scenario**: a direct gateway request with no upstream trace still produces `GATEWAY router → GATEWAY attempt-1` with Router as Root (no fabricated LLM/AGENT spans).

## Capabilities

### New Capabilities

- `gateway-observability-contract`: The semantic contract — Router/Attempt span hierarchy and attributes, fixed event names, error taxonomy, Usage/Cost ownership across Attempt/Router/LLM, streaming lifecycle, sampling inheritance, privacy rules, and fail-open semantics.
- `gateway-observability-runtime`: The runtime implementation — RouterSpan/AttemptSpan lifecycle, context/registries, GatewayEventRecorder, UsageNormalizer/CostCalculator, PrivacyGuard, streaming wrapper, and propagation helper.
- `oneapi-adapter`: The One-API glue layer — request/channel/route/retry/response/usage mapping plus the adapter boundary constraints (no core routing mutation, no business-exception swallowing).

### Modified Capabilities

- `langchain-observability`: SDK LLM span now takes its final Usage from the Router aggregate when a Gateway Router span exists (instead of only the successful attempt's usage); the existing SDK LLM span remains the logical-call owner.

## Impact

- **SDK** (`sdk/python/llm_observability/`): new `gateway_observability/` package (context, runtime, router_span, attempt_span, adapter, attributes, events, usage, cost, errors, privacy, streaming, propagation); LLM span usage aggregation hook; span registry integration for gateway context cleanup. No new SpanKind.
- **Integrations**: new `integrations/oneapi/` (adapter, request_mapper, response_mapper, channel_mapper, retry_mapper, usage_mapper); new reserved `integrations/litellm/adapter.py`.
- **Proxy/Core**: no mandatory change this round; proxy remains the existing GATEWAY producer path; Core keeps using the existing Span table with JSON attributes (materialized gateway columns only after a performance verification, per spec §21).
- **Tests**: new `tests/gateway_observability/` suite (router_span, attempt_span, retry, fallback, streaming, usage, cost, privacy, sampling, fail_open, registry_cleanup, oneapi_adapter) plus Success/Retry mock E2E.
- **CI**: new `gateway-runtime-tests`, `oneapi-adapter-tests`, `gateway-streaming-tests`, `gateway-real-e2e` (trusted-branch-only, secret-gated) jobs; Phase 2.1–2.5 regression kept green.
- **No breaking API changes** to the existing SDK public surface beyond GATEWAY spans gaining role attributes and the Router hierarchy.
