## Context

Phase 2.5 introduced the AgentLens SDK parity surface (decorators, association, distributed, instruments, LangChain auto). The closeout review at `37ae49f` found six P0 freezing blockers and five P1 closures. The codebase spans SDK (`sdk/python/llm_observability`), Proxy (`proxy/`), Core (`core/`), UI (`ui/`), and tests. LangChain is pinned at `langchain==1.3.14` / `langchain-core==1.5.1` / `langgraph==1.2.9` / `langchain-openai==1.4.0`.

Key current-state defects: LangChain auto only patches `Runnable` base methods (subclass overrides bypass it; `_merge_callback` mutates user Config in place; handler state leaks across invocations); TASK/TOOL `__exit__` lets `span.end()`/`set_error()`/`to_record()` failures replace business exceptions; Association is applied inconsistently and `@agent` explicit params don't establish a context for children; module-level constants override runtime `Config`; baggage encode/decode logic is duplicated across 3 modules with divergent semantics.

## Goals / Non-Goals

**Goals:**
- Reliable LangChain auto-instrumentation for Direct ChatModel, RunnableLambda/Sequence/Parallel, create_agent, CompiledGraph across invoke/ainvoke/stream/astream.
- Per-invocation isolation: fresh handler/registry/state per root invocation; nested calls reuse the root's state.
- Complete fail-open: telemetry failures never alter business result or exception for TASK/TOOL.
- Single Association resolver with frozen priority applied to all span-creation paths.
- Single baggage contract reused by SDK/OpenAI/Proxy.
- Runtime `max_payload_bytes`/`max_attribute_bytes`/`fail_open` enforced everywhere.
- Real E2E + CI visibility + Phase 2.1–2.4 regression green.

**Non-Goals:**
- Phase 3 Gateway Native Observability, LangGraph advanced workflows, Embedding/Retrieval/Rerank, Qdrant/Bedrock, more framework auto-instrumentation.

## Decisions

### D1: LangChain auto via contextvar per-invocation state + Runnable patch + callback injection
Keep patching `Runnable.invoke/ainvoke/stream/astream` BUT introduce a `ContextVar[AutoInvocationState]` holding `(handler, root_trace_cm, depth)`. Root call (depth 0, no active trace): create handler + AGENT root, set contextvar. Nested call (depth>0 or active trace): reuse state, depth++. Exit root: close runs, end AGENT, clear contextvar. This survives subclass overrides because `invoke` is the public entry resolved on the class at call time, and nested `_invoke` calls that go through `invoke` reuse the state. User Config is deep-copied before mutating `callbacks`. Alternatives considered: patching `ensure_config` only (import-order fragile, rejected); patching every Runnable subclass instance (forbidden by spec).

### D2: TASK/TOOL fail-open structure
Adopt the doc's prescribed nested try/except: outer `try` holds all telemetry steps in an inner `try/except Exception` (log + swallow), `finally` does `safe_unregister_event_sink()` + `safe_reset_context()` (each itself wrapped in try/except). `__enter__` records cleanup handles (event sink registered flag, token) and on any mid-creation failure runs the same cleanup. This guarantees business result/exception primacy.

### D3: Single Association resolver
`resolve_association(span_explicit, decorator_explicit, context, remote)` returns a frozen `AssociationProperties`. All span-creation paths call `apply_resolved_association(span, resolved)` instead of ad-hoc `apply_association_to_span`. `@agent` explicit params call `set_association_properties(...)` to establish a temporary context (token reset in finally) so children inherit via the context priority. Priority frozen: span explicit > decorator explicit > association context > remote carrier > None.

### D4: Unified baggage module
New `association_propagation.py` exposing `encode_baggage_value`/`decode_baggage_value`/`build_association_baggage`/`parse_association_baggage`/`merge_remote_association`. `distributed.py`, `propagation.inject_headers`, and `proxy/trace_context.extract_metadata_headers` all import/reuse it. Proxy cannot import SDK (separate process) → copy the pure functions as a shared contract with identical tests. Priority frozen: local span explicit > local association context > Compat header > W3C baggage > None.

### D5: Runtime config plumbing
Thread `tracer.config.max_payload_bytes` into every `apply_size_guard` call and `BoundedStreamAccumulator(max_bytes=...)`. Thread `tracer.config.max_attribute_bytes` into `_apply_size_limit_to_value`/`set_attribute` paths. Module constants remain only as tracer-less fallbacks. Decorator `fail_open` default becomes `None`; `_resolve_fail_open(None)` reads `Config.fail_open`. Add `max_payload_bytes` upper bound 16 MiB in init validation.

### D6: Annotate closure
Neutral keys `sdk.annotation.input.truncated`/`sdk.annotation.output.truncated` (+ `original_size_bytes`). Tags: `safe_serialize` + masking + `max_attribute_bytes` + ≤32 tags + ≤256 chars/tag. `annotate()` checks `span.end_time is None` for explicit spans and returns False for ended/unregistered spans.

### D7: LangChain auto default
Freeze as `auto_instrument_langchain=False` (opt-in), documented explicitly. Rationale: optional dependency must not auto-activate and risk patching on import; users opt in. Documents state the required `init(auto_instrument_langchain=True)`.

## Risks / Trade-offs

- [LangChain subclass overrides bypass Runnable patch] → Mitigation: contextvar state means even if a subclass overrides `invoke`, the user's own `invoke` still calls into our patched base via `super()` in common cases; E2E tests cover Direct Model/Sequence/Agent. Document the supported surface.
- [Per-invocation contextvar adds overhead] → Mitigation: contextvar lookup is O(1) and only on root entry/exit; negligible vs LLM latency.
- [Proxy can't import SDK baggage module] → Mitigation: duplicate pure functions with shared contract tests asserting byte-identical encode/decode.
- [fail-open hides real telemetry bugs] → Mitigation: every swallowed step logs at ERROR with stack trace; fault-injection tests assert business primacy AND that telemetry failure is logged.
- [Runtime config changes after init not hot-reloadable] → Accepted; config is init-time. Document.

## Migration Plan

No public API breaks. Behavior hardens (more fail-open, more consistent association, runtime limits enforced). Deploy is a drop-in SDK upgrade. Rollback = revert SDK package version. No storage migration (message_id/chain_count already shipped).
