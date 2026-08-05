## Why

Phase 2.5 (AgentLens SDK Parity) shipped its framework, but the final closeout review (`37ae49f`) found freezing blockers: LangChain auto-instrumentation is unreliable and lacks invocation isolation, TASK/TOOL finalization can leak telemetry exceptions into business, Association propagation is inconsistent across span kinds, runtime config (`max_payload_bytes`/`max_attribute_bytes`/`fail_open`) is silently ignored, and no real E2E validates the full call chain. Phase 2.5 cannot be marked COMPLETE/FROZEN until these are closed.

## What Changes

- **P0-1 LangChain Auto reliability**: replace bare `Runnable` base-class patching with a contextvar-driven per-invocation state model that survives subclass overrides; copy user Config (no in-place mutation); fresh `CallbackHandler`/registry per root invocation; preserve user callbacks (list/CallbackManager/AsyncCallbackManager); hard dedup (1 LLM/TOOL/GATEWAY per attempt, ≤1 AGENT per root).
- **P0-2 Complete Fail-open**: TASK/TOOL `__exit__` wraps all telemetry steps (set_error/set_status/end/output/metadata/to_record/report/reset_context) in inner try/except so business result/exception is never altered; `__enter__` failure cleans up event sink + context token; `reset_context` itself is fail-open.
- **P0-3 Association full-chain**: single `resolve_association` resolver with frozen priority (span explicit > decorator explicit > context > remote > None); `@agent` explicit params establish a temporary Association Context inherited by all child spans; LangChain Callback LLM/TOOL/Retriever apply the resolver.
- **P0-4 Unified Baggage contract**: new `association_propagation.py` module with one `encode/decode/build/parse/merge` API reused by SDK Distributed, OpenAI propagation, and Proxy; Compat-header-over-baggage priority; fail-closed sanitization.
- **P0-5 Runtime config enforcement**: all payload paths use `tracer.config.max_payload_bytes`, all attribute paths use `tracer.config.max_attribute_bytes`; `BoundedStreamAccumulator` takes explicit budget; decorator `fail_open` defaults to `None` (resolves to global Config); `max_payload_bytes` upper bound 16 MiB.
- **P0-6 Real E2E**: manual-decorator, LangChain-auto, Association full-chain, cross-service, sampling, and streaming E2E scenarios.
- **P1-1 Annotate closure**: neutral truncation keys (`sdk.annotation.*`); tags privacy+size limits (≤32 tags, ≤256 chars); reject ended/unregistered spans.
- **P1-2 Streaming tests**: assert first-chunk-immediate via `next()`/`__anext__` (not `list()`); cover close/aclose/break/CancelledError.
- **P1-3 Registry cleanup**: stress-test event-sink/registry/ContextVar cleanup after N invocations.
- **P1-4 GitHub CI**: visible status checks (sdk/core/proxy/ui/e2e/regression) on the target commit.
- **P1-5 Freeze LangChain auto default**: decide and document `auto_instrument_langchain` default.

## Capabilities

### New Capabilities

- `association-propagation`: Unified baggage encode/decode/build/parse/merge contract shared across SDK Distributed, OpenAI propagation, and Proxy with frozen Compat-over-baggage priority and fail-closed sanitization.
- `langchain-auto-instrumentation`: Reliable LangChain auto-instrumentation with per-invocation isolation (contextvar state), non-destructive user Config copy, callback preservation, and hard dedup against explicit/middleware/OpenAI modes.

### Modified Capabilities

- `langchain-observability`: TASK/TOOL complete fail-open, Association full-chain inheritance from `@agent` explicit params to all span kinds + Gateway, runtime config enforcement (`max_payload_bytes`/`max_attribute_bytes`/`fail_open`), annotate neutral keys + lifecycle protection, streaming lifecycle, and registry cleanup requirements.

## Impact

- **SDK**: `decorators.py`, `task.py`, `tool.py`, `annotation.py`, `association.py`, `distributed.py`, `propagation.py`, `config.py`, `instrumentation/langchain.py`, `instrumentation/openai.py`, `integrations/langchain/callback_handler.py`, `integrations/langchain/callback_spans.py`; new `association_propagation.py`.
- **Proxy**: `trace_context.py` (baggage decode), `handler.py`.
- **Tests**: 7 new test files (langchain-auto-real, fail-open-fault-injection, runtime-config, association-full-chain, streaming-lifecycle, registry-cleanup, real-e2e).
- **CI**: new GitHub Actions workflow files.
- **Docs**: LangChain auto default + user docs freeze.
- **No breaking API changes** to public surface; behavior hardens toward fail-open and consistency.
