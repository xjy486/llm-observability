## Context

Phase 2.5 is frozen (`63c148a`). The SDK (`sdk/python/llm_observability/`) can emit `GATEWAY` spans (`SpanKind.GATEWAY` exists in `spans.py`), and the Proxy (`proxy/handler.py`) creates a single flat GATEWAY span per request — but that span cannot explain gateway-internal decisions. Phase 3 adds a gateway-decoupled **Gateway Native Observability Contract**: Router + per-attempt Provider spans over the existing SpanKind set, unified events, error taxonomy, three-layer Usage/Cost ownership, streaming lifecycle, sampling inheritance, privacy, and fail-open. First round delivers the contract + runtime skeleton + Generic adapter + Success/Retry mock E2E + Privacy/Sampling/Fail-open unit tests; One-API full adapter, UI, and pricing tables are deferred to Phase 3.1.

Constraints from the codebase:
- No new SpanKind (spec §4.1): roles via `gateway.span_role = router | provider_attempt`.
- Spans are plain dataclasses (`spans.Span`) reported via `Reporter`; context via `context.SpanContext` + `set_context/reset_context`; sampling via `traceparent` `trace_flags`.
- `proxy/handler.py` is the existing flat GATEWAY producer; `integrations/langchain/` is the LangChain callback surface; `instrumentation/openai.py` already has `ObservedStream` / `AsyncObservedStream` streaming wrappers whose finalization patterns the new streaming wrapper should mirror.
- Telemetry must be fail-open everywhere (Phase 2.5 closeout precedent) and must not modify gateway business semantics.

## Goals / Non-Goals

**Goals:**
- Freeze Router/Attempt GATEWAY semantics and attributes; verify hierarchy in one trace.
- Generic `GatewayAdapter` abstraction that One-API and LiteLLM adapters can implement without touching gateway runtime internals.
- Router + Attempt runtime with retry/fallback/cache/rate-limit event recording; no Attempt reuse.
- Streaming spans live through the real terminal state (full consumption, `[DONE]`, client cancel, close/aclose, upstream error).
- Usage/Cost owned at Attempt → Router → SDK LLM with retry cost preserved.
- Privacy, sampling inheritance, and fail-open backed by unit tests + Success/Retry mock E2E.

**Non-Goals:**
- Modifying One-API core routing, retry counts, timeouts, or quota.
- Auto channel selection / auto degradation policy.
- One-API full adapter, LiteLLM implementation, UI Router/Attempt rendering, pricing tables, Anthropic Messages / OpenAI Responses usage (all Phase 3.1).
- New SpanKind, LangGraph checkpoint/interrupt, Embedding/Rerank/Vector DB, MCP.
- Materialized gateway columns in Core storage (deferred until JSON query perf is verified, spec §21).

## Decisions

### D1: New `gateway_observability/` package in the SDK, adapter-side decoupled
New package `sdk/python/llm_observability/gateway_observability/` with `context.py`, `runtime.py`, `router_span.py`, `attempt_span.py`, `adapter.py`, `attributes.py`, `events.py`, `usage.py`, `cost.py`, `errors.py`, `privacy.py`, `streaming.py`, `propagation.py`. The runtime depends only on the adapter interface (`GatewayAdapter` ABC), never on a concrete gateway. One-API glue lives in `integrations/oneapi/`; LiteLLM gets a reserved `integrations/litellm/adapter.py`. Alternative considered: keep gateway runtime in proxy — rejected because the contract must serve both SDK-attached and no-SDK gateway traces, and the SDK already owns span/report primitives.

### D2: Role differentiation via `gateway.span_role`, no new SpanKind
Router and Attempt are both `SpanKind.GATEWAY`; the role is `gateway.span_role`. Router's parent is the SDK LLM span when present, else it is the Root. Attempt's parent is always the Router. This satisfies spec §4.1 and keeps Core/UI queries simple (`span_kind + attributes.gateway.span_role`). Alternative (new `ROUTER`/`PROVIDER` kinds) rejected by spec §4.1 and would ripple through Core/UI/SDK filters.

### D3: `RouterSpan` / `AttemptSpan` context managers over a single `Span` each
Each runtime object wraps one `spans.Span` dataclass, one `context.SpanContext` push/pop token, and one attempt registry entry. `RouterSpan.attempt(...)` returns an `AttemptSpan` context manager whose parent is fixed to the Router. Both remain open until the streaming wrapper or context-manager exit reaches a terminal state. Span creation, event add, usage parse, cost calc, and end are each wrapped in inner try/except (fail-open), matching the Phase 2.5 TASK/TOOL pattern. Alternative: one big manager owning both — rejected because retry needs discrete per-attempt lifecycles and event timing.

### D4: Per-request registries + ContextVar cleared on every terminal path
A `GatewayContext` ContextVar holds `(router, active_attempt)`; a `RouterRegistry` / `AttemptRegistry` (or one gateway registry) keys spans by `request_id`/`span_id`. Cleanup runs in `finally`/`close`/`aclose` on success, error, client cancel, `GeneratorExit`, `aclose`, and span-end failure. Streaming wrapper and background-task handles are also released. Mirrors the SDK event-sink/registry cleanup precedent from Phase 2.5 closeout. No path may leave a stale ContextVar or registry entry.

### D5: `GatewayEventRecorder` with a fixed event/attribute whitelist
Events use the fixed `gateway.*` names from the contract spec; attributes are restricted to `reason`, `attempt_index`, `channel_id`, `provider`, `resolved_model`, `delay_ms`, `error_category`, `http_status_code`. No raw payload by default. Recorder wraps `span.add_event` in try/except.

### D6: `UsageNormalizer` (OpenAI-compatible) + `CostCalculator` with fail-open
`UsageNormalizer.normalize(raw_usage, source)` maps OpenAI/OpenAI-compatible fields (`prompt_tokens`/`completion_tokens`/`total_tokens`, plus cache/reasoning variants when present) into `NormalizedUsage`. Anthropic/Responses parsing is deferred (Phase 3.1). `CostCalculator` maps normalized usage to `NormalizedCost`; without a pricing table it emits `cost.source="unpriced"` and `total_cost=None` rather than failing. Any parse/calc exception is swallowed and logged; the span ends with what was recorded. Alternatives: strict cost table now — rejected as Phase 3.1 scope.

### D7: `PrivacyGuard` default-deny + channel ID hashing
A `sanitize` layer runs before any attribute/event/error-message write: blocks Authorization/API Key/Cookie/Set-Cookie/raw secret/full URL query/full prompt/response/tool I/O/uploads; allows provider/model/status/usage/cost/error category/request IDs. Channel IDs are hashed via HMAC at the adapter boundary. Fail-closed: a masking failure yields `<redacted>` and still allows the span to proceed.

### D8: Sampling inheritance, no gateway re-sampling
When a legitimate `traceparent` exists, `trace_flags` decides; the gateway never re-randomizes. With no upstream trace, the runtime consults a local `sample_rate` to create a Root Router. Sampled-out: business runs, `traceparent` still propagates, no Reporter Record, no large serialization. Reuses the `proxy/trace_context` decision precedent.

### D9: Streaming wrapper mirrors the existing `ObservedStream` finalization but keeps spans open to the real end
`gateway_observability/streaming.py` wraps both sync and async generators. Span end happens only at full consumption / `[DONE]` / client cancel/close / upstream timeout or connection error / `close()` / `aclose()`. TTFT recorded exactly once. On cancel, both Router and Attempt end with `error_category = client_cancelled` and all registry/ContextVar/wrapper handles are released. This borrows the lifecycle discipline from `instrumentation/openai.py` `ObservedStream`/`AsyncObservedStream`.

### D10: SDK LLM usage aggregation hook
When a Router exists for a logical call, the SDK LLM span's final Usage equals the Router aggregate. The aggregation hook lives where the LLM span finalizes (`instrumentation/openai.py` / LangChain callback) and reads the current `GatewayContext`; if no Router, the LLM keeps its own usage. This satisfies the `langchain-observability` delta requirement.

### D11: One-API adapter boundary
`integrations/oneapi/` maps One-API request token → association, Channel → provider/channel fields (hashed), model mapping → requested/resolved, relay mode → protocol, retry → new Attempt + event, fallback → event + new Attempt, quota → usage/cost, upstream response → Attempt result. The adapter implements only `GatewayAdapter` and never mutates routing/retry/timeout/quota or swallows business exceptions. Integration into the One-API server itself is a Phase 3.1 hook/middleware concern; this round ships the adapter + mappers + unit tests against mock internal state.

### D12: First-round E2E via a mock gateway harness
A small mock gateway harness exercises the runtime directly (not through One-API): Success, Retry (500→200), Fallback (timeout→success), Cache hit, Rate limit, Streaming success/cancel, No-SDK root. These are the "Real Gateway E2E" acceptance proxies for round one, since One-API real integration is Phase 3.1.

## Risks / Trade-offs

- [Attempt/Router spans kept open during long streams → memory/registry pressure] → Mitigation: per-terminal-path cleanup guarantees, streaming tests assert zero residual registry entries/ContextVars after close/cancel.
- [Router/Attempt attributes inflate span size] → Mitigation: fixed attribute whitelist, no raw payloads, `max_attribute_bytes`/`max_payload_bytes` config applied; size limits inherited from SDK Config.
- [Fail-open can hide real telemetry bugs] → Mitigation: every swallowed step logs at ERROR; fault-injection tests assert business primacy AND that the failure is logged.
- [Proxy is a separate process and cannot import SDK gateway runtime] → Mitigation: round one delivers the runtime as SDK-side library + mock E2E; the Proxy migration to the contract (Router under existing GATEWAY flow) is a follow-up that reuses the same contract module by vendoring the pure contract definitions — flagged as an open question below.
- [Channel ID hashing makes UI debugging harder] → Accepted per spec §16; UI can display hashed IDs.
- [Cost without pricing table is partial] → Accepted; `cost.source="unpriced"` is explicit and Phase 3.1 adds pricing tables.
- [Router aggregate must never double-count when an LLM already holds usage] → Mitigation: LLM usage = Router aggregate only when a Router exists; ownership boundary is explicit in D10.

## Migration Plan

Round one adds a new SDK package plus new tests — no public API breaks, no storage migration (Core keeps the existing Span table with JSON attributes). Rollback = drop the new package and its hook call sites. The existing flat proxy GATEWAY span continues to work; it does not emit Router/Attempt roles until a later round adopts the contract. Phase 2.1–2.5 regression suites must stay green in CI (`phase2-regression` job).

## Open Questions

- Should the Proxy adopt the Router/Attempt contract in round one (SDK-side only) or in a follow-up? The spec's acceptance is gateway-real E2E; round one proves it with the mock harness and defers the Proxy migration.
- Exact placement of the SDK LLM → Router aggregation hook: instrument OpenAI path only, or also LangChain callback LLM path in round one? Default: instrument both where the LLM span finalizes.
- `GatewayAdapter.extract_route_decision` input type: `Any internal_state` is intentionally loose — needs a documented minimal internal-state contract per gateway so One-API adapter tests are realistic.
