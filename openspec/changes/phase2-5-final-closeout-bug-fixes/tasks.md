# Implementation Tasks

## 1. Complete Fail-open (P0-2)

- [x] 1.1 Refactor TASK `__exit__` to nested try/except: inner try wraps set_error/set_status/end/output/metadata/to_record/report (log+swallow), finally does safe_unregister_event_sink + safe_reset_context (each wrapped)
- [x] 1.2 Refactor TOOL `__exit__` with the same structure
- [x] 1.3 Add `__enter__` failure cleanup in TASK/TOOL: track event-sink-registered flag + token; on mid-creation failure unregister sink + reset token + best-effort end
- [x] 1.4 Make `reset_context` itself fail-open (try/except + best_effort_restore_parent)
- [x] 1.5 Write fault-injection tests `test_phase2_5_fail_open_fault_injection.py` (TASK+TOOL: set_error/set_status/end/output/to_record/report/register/set_context/unregister/reset_context/`__str__` failures, success+error paths)

## 2. Runtime Config Enforcement (P0-5)

- [x] 2.1 Thread `tracer.config.max_payload_bytes` into all `apply_size_guard` calls (decorators agent/llm, task, tool, callback LLM, retriever, annotate, stream accumulator)
- [x] 2.2 Thread `tracer.config.max_attribute_bytes` into set_attribute/add_event/annotate attribute paths
- [x] 2.3 Construct `BoundedStreamAccumulator(max_bytes=tracer.config.max_payload_bytes)` in all generator wrappers
- [x] 2.4 Change decorator `fail_open` defaults to `None`; ensure `_resolve_fail_open(None)` reads `Config.fail_open`
- [x] 2.5 Add `max_payload_bytes` upper bound (16 MiB) validation in init
- [x] 2.6 Write `test_phase2_5_runtime_config.py` (payload/attribute limits, fail_open=None→global, explicit override)

## 3. Association Full-Chain (P0-3)

- [x] 3.1 Add `resolve_association(span_explicit, decorator_explicit, context, remote)` to association.py with frozen priority
- [x] 3.2 `@agent` explicit params establish temporary Association Context (set_association_properties token, reset in finally incl. generators)
- [x] 3.3 Apply resolver in Callback LLM (`callback_spans.py` CallbackLLMSpan.__enter__), Callback TOOL, Retriever TOOL
- [x] 3.4 Ensure Distributed Server AGENT applies resolver (local override of remote)
- [x] 3.5 Write `test_phase2_5_association_full_chain.py` (agent explicit→task/tool/llm/gateway, nested merge, restored after success/error/generator-close/aclose, callback LLM/tool, distributed server override)

## 4. Unified Baggage Contract (P0-4)

- [x] 4.1 Create `sdk/python/llm_observability/association_propagation.py` with encode/decode/build/parse/merge_remote
- [x] 4.2 Refactor `distributed.py` inject/extract_carrier to reuse the module
- [x] 4.3 Refactor `propagation.inject_headers` to reuse build_association_baggage
- [x] 4.4 Copy pure baggage functions to `proxy/` (or shared contract) and refactor `proxy/trace_context.extract_metadata_headers` to percent-decode
- [x] 4.5 Write baggage contract tests (special chars, unicode, percent, control, compat-over-baggage, carrier-excludes-secrets, fail-closed masking)

## 5. LangChain Auto Reliability (P0-1)

- [x] 5.1 Introduce `ContextVar[AutoInvocationState]` (handler, root_trace_cm, depth) in instrumentation/langchain.py
- [x] 5.2 Root invocation (depth 0, no active trace): create fresh handler + AGENT root, set contextvar; nested (depth>0/active trace): reuse state, depth++
- [x] 5.3 Non-destructive Config copy in `_merge_callback` (deep copy before mutating callbacks)
- [x] 5.4 Preserve user callbacks for None/list/CallbackManager/AsyncCallbackManager (wrap, don't replace)
- [x] 5.5 Exit root: close_open_runs, end AGENT, clear contextvar
- [x] 5.6 Verify dedup vs observe_runnable/observe_agent/middleware/user-callback/OpenAI (1 LLM/TOOL/GATEWAY per attempt, ≤1 AGENT per root)
- [x] 5.7 Freeze `auto_instrument_langchain=False` default + document opt-in
- [x] 5.8 Write `test_phase2_5_langchain_auto_real.py` (import before/after init, direct model, lambda/sequence/parallel, create_agent, stream/astream, same-config sequential/concurrent, thread/async concurrency, callback preservation, dedup, shutdown restores, reinit)

## 6. Annotate Closure (P1-1)

- [x] 6.1 Switch annotate truncation keys to neutral `sdk.annotation.*` (+ original_size_bytes)
- [x] 6.2 Tags: safe_serialize + masking + max_attribute_bytes + ≤32 tags + ≤256 chars/tag
- [x] 6.3 annotate() checks `span.end_time is None` for explicit spans; returns False for ended/unregistered
- [x] 6.4 Write annotate closure tests (neutral keys, tags masked/size-guarded, reject ended explicit span, current span after end returns False)

## 7. Streaming Lifecycle Tests (P1-2)

- [x] 7.1 Write `test_phase2_5_streaming_lifecycle.py` asserting first-chunk-immediate via next()/__anext__ (not list())
- [x] 7.2 Cover close()/aclose()/break/CancelledError/infinite-stream-fixed-count (context restored, no ERROR, sink unregistered, bounded)

## 8. Registry Cleanup (P1-3)

- [x] 8.1 Write `test_phase2_5_registry_cleanup.py` (event sink registry empty after 1000 calls for task/tool/llm; langchain handler registry + _spans_by_id empty; stream close releases refs; 10k stress → registry size 0)

## 9. Real E2E (P0-6)

- [x] 9.1 Write `test_phase2_5_real_e2e.py` Scenario A (manual decorator: AGENT→TASK→LLM→GATEWAY, parent chain, dedup, association, sampling, payload)
- [x] 9.2 Scenario B (LangChain auto: direct model/sequence/parallel/agent, invoke/ainvoke/stream/astream, dedup, user callback called)
- [x] 9.3 Scenario C (association full-chain: user/session/message_id/business_scenario identical across AGENT/TASK/TOOL/LLM/GATEWAY)
- [x] 9.4 Scenario D (cross-service: client TASK + server AGENT same trace, parent linkage, remote sampling, association inheritance, local override)
- [x] 9.5 Scenario E (sampling: sample_rate=0 → no SDK records, traceparent trace_flags=00, proxy sampled=0, no big payload serialization)
- [x] 9.6 Scenario F (streaming: all generator variants, first-chunk-immediate, duration covers consumption, close/aclose correct, GeneratorExit/CancelledError not ERROR, context restored, bounded)

## 10. GitHub CI + Regression (P1-4)

- [x] 10.1 Add GitHub Actions workflow: sdk-tests, core-tests, proxy-tests, ui-typecheck, phase2.5-real-e2e, phase2.1-2.4-regression
- [x] 10.2 Add secret E2E job (trusted branches/manual, API key from secrets, no key in logs, fork PR excluded, log redaction)
- [x] 10.3 Run full Phase 2.1–2.4 regression suite green
- [x] 10.4 Commit all changes to main and push; confirm CI visible + green on target commit

## 11. Freeze

- [x] 11.1 Mark Phase 2.5 COMPLETE/FROZEN in docs/openspec after all DoD met
