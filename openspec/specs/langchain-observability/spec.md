# LangChain Observability Specification

## Purpose

Defines requirements for the LangChain observability integration, covering callback inheritance, span lifecycle management, async context restoration, and payload strategy for custom events.
## Requirements
### Requirement: Callback handlers supplied through a CallbackManager are inherited by child runs

When a Runnable receives a user-owned `CallbackManager`, the wrapper MUST clone it, preserve user handlers, add the observability handler to both handler collections, and leave the original manager unchanged.

#### Scenario: Model callback is inherited

- **WHEN** a wrapped Runnable is invoked with a `CallbackManager`
- **THEN** the child model callback creates exactly one LLM span
- **AND** the original manager has no accumulated observability handler

#### Scenario: Tool and retriever callbacks are inherited

- **WHEN** a child Tool or Retriever run is dispatched from the cloned manager
- **THEN** the observability callback receives the child lifecycle events
- **AND** the original manager remains unchanged

### Requirement: Callback-created event sinks and local references have bounded lifetimes

Tool, Retriever, and LLM callback spans MUST remove both the local `_spans_by_id` reference and the global event sink after success, error, span-end failure, or forced handler cleanup. A long-lived manual handler reused across traces MUST NOT accumulate ended span references.

#### Scenario: Normal and error cleanup

- **WHEN** a Tool, Retriever, or LLM callback completes or errors
- **THEN** its span is absent from the handler's local `_spans_by_id` map
- **AND** its event sink is absent from the global sink registry

#### Scenario: Forced cleanup and LLM end failure

- **WHEN** a handler closes an unfinished run or an LLM span end raises
- **THEN** the local span reference and global event sink are both removed

#### Scenario: Manual handler reuse

- **WHEN** a long-lived manual handler is reused across multiple traces
- **THEN** `len(handler._spans_by_id)` is `0` after each trace completes

### Requirement: Async callback finalization restores the parent context

When an LLM callback token cannot be reset because start and end execute in different async contexts, finalization MUST restore the recorded parent context in the current context.

#### Scenario: Cross-context LLM end

- **WHEN** an LLM starts in one async context and ends in another
- **THEN** the current context after finalization is the parent AGENT context

#### Scenario: Async Runnable lifecycle

- **WHEN** parallel branches, consecutive `ainvoke` calls, or an early-closed `astream` complete
- **THEN** no stale LLM context remains in the caller

### Requirement: Payload-off custom events retain controlled event metadata

When payload capture is disabled, custom events MUST retain a controlled event name while omitting event data.

#### Scenario: Payload strategy off

- **WHEN** a custom event is emitted with `payload_strategy=off`
- **THEN** the normalized custom event name is recorded
- **AND** event data attributes are absent

### Requirement: SDK LLM span usage aggregates gateway router usage

When a Gateway Router span exists for an SDK LLM logical call, the SDK LLM span SHALL record the Router aggregate Usage (sum over all attempts, including failed ones) as its final Usage, and SHALL NOT record only the final successful Attempt's Usage. The SDK LLM span remains the owner of the logical call's final Usage and Timing; the Router remains the owner of the per-gateway decision aggregate.

#### Scenario: LLM usage equals router aggregate after retry

- **WHEN** an SDK LLM call routes through a gateway whose Router aggregated Usage across a failed attempt and a successful attempt
- **THEN** the SDK LLM span's final Usage equals the Router aggregate Usage
- **AND** the LLM span does not carry only the successful attempt's Usage

#### Scenario: LLM span without router keeps its own usage

- **WHEN** an SDK LLM call has no Gateway Router span
- **THEN** the SDK LLM span records its own directly-measured Usage and is unchanged by this requirement

### Requirement: TASK/TOOL complete fail-open
TASK and TOOL span finalization SHALL wrap every telemetry step (set_error, set_status, span.end, output processing, request_metadata processing, to_record, reporter.report) in an inner try/except that logs and swallows, so a telemetry failure never alters the business result or replaces the business exception. `reset_context` SHALL itself be fail-open (wrapped in try/except). On `__enter__` mid-creation failure, any registered event sink SHALL be unregistered and any created context token SHALL be reset before propagating.

#### Scenario: span.end failure preserves success result
- **WHEN** a TASK/TOOL business call succeeds but `span.end()` raises
- **THEN** the business result is returned unchanged and no exception reaches the caller

#### Scenario: span.end failure preserves business error
- **WHEN** a TASK/TOOL business call raises ValueError and `span.end()` raises RuntimeError
- **THEN** the caller receives the original ValueError, not the RuntimeError

#### Scenario: set_context failure unregisters event sink
- **WHEN** `set_context` raises after an event sink was registered in a TASK/TOOL enter
- **THEN** the event sink is unregistered and no stale sink remains

#### Scenario: reset_context failure does not replace business error
- **WHEN** `reset_context` raises during a TASK/TOOL exit that already has a business exception
- **THEN** the original business exception propagates

### Requirement: Association full-chain inheritance
`@agent` explicit parameters (`user_id`, `session_id`, `message_id`, `business_scenario`) SHALL establish a temporary Association Context for the duration of the agent invocation. All child spans (TASK, TOOL, LLM, GATEWAY) created within the agent SHALL inherit these association values. The temporary context SHALL be restored on success, error, GeneratorExit, and async generator aclose. LangChain Callback LLM/TOOL and Retriever TOOL spans SHALL apply the unified Association resolver.

#### Scenario: agent explicit association inherited by task
- **WHEN** `@agent(user_id="alice", session_id="s1", message_id="m1")` wraps a function that creates a TASK span
- **THEN** the TASK span has `user_id="alice"`, `session_id="s1"`, `message_id="m1"`

#### Scenario: agent explicit association inherited by gateway
- **WHEN** an agent with explicit association triggers an OpenAI call that produces a GATEWAY span via the proxy
- **THEN** the GATEWAY span record carries the same `message_id`

#### Scenario: association restored after generator close
- **WHEN** an agent async generator is closed early via `aclose()`
- **THEN** the temporary association context is restored to its pre-agent state

### Requirement: Runtime config enforcement
All payload capture paths (agent/llm/task/tool input/output, callback LLM, retriever content, annotate, streaming accumulator) SHALL use `tracer.config.max_payload_bytes` as the size-guard budget. All attribute paths (TaskHandle/ToolHandle.set_attribute, add_event, annotate attributes, LangChain tags/metadata) SHALL use `tracer.config.max_attribute_bytes`. `BoundedStreamAccumulator` SHALL be constructed with `max_bytes=tracer.config.max_payload_bytes`. Decorator `fail_open` SHALL default to `None` and resolve to `Config.fail_open` when `None`. `max_payload_bytes` SHALL be validated to an upper bound of 16 MiB at init.

#### Scenario: custom max_payload_bytes respected
- **WHEN** `init(max_payload_bytes=2048)` and an agent captures a 10 KiB output
- **THEN** the captured payload is truncated to 2048 bytes and `truncated=True`

#### Scenario: default fail_open uses global false
- **WHEN** `init(fail_open=False)` and `@agent()` (fail_open defaults to None) runs without an SDK init in a no-trace context
- **THEN** the decorator raises RuntimeError per the global config, not fail-open

### Requirement: Annotate neutral keys and lifecycle protection
`annotate()` SHALL write truncation metadata under neutral keys `sdk.annotation.input.truncated`, `sdk.annotation.input.original_size_bytes`, `sdk.annotation.output.truncated`, `sdk.annotation.output.original_size_bytes` regardless of span kind. Tags SHALL pass through safe_serialize, masking, and `max_attribute_bytes`, with at most 32 tags and 256 characters per tag. `annotate()` SHALL return False without modifying the span when the target span has ended (`end_time is not None`) or is unregistered.

#### Scenario: annotate uses neutral truncation keys
- **WHEN** `annotate(input_data=...)` is called on an active span
- **THEN** the span attributes contain `sdk.annotation.input.truncated` and not `task.input.truncated`

#### Scenario: annotate rejects ended span
- **WHEN** `annotate(span=ended_span)` is called where `ended_span.end_time` is set
- **THEN** annotate returns False and the span is not modified

### Requirement: Streaming lifecycle correctness
Streaming decorators SHALL yield the first chunk immediately (before exhausting the generator). On early `close()`/`aclose()`, `break`, or `CancelledError`, the span SHALL end without marking a normal ERROR, the context SHALL be restored, and the event sink SHALL be unregistered. The accumulator SHALL stay bounded by the configured budget.

#### Scenario: first chunk immediate
- **WHEN** a `@task()` sync generator yields "first" then sets a flag before yielding "second"
- **THEN** `next(gen)` returns "first" while the flag is still False

#### Scenario: early close restores context
- **WHEN** an async generator is consumed once then `aclose()` is awaited
- **THEN** no active span context remains and no event sink is registered for that span

### Requirement: Registry and context cleanup
After a span ends, its event sink SHALL be unregistered from the Span Event Sink Registry, the LangChain CallbackRunRegistry and handler `_spans_by_id` SHALL be empty for that run, and the Association/Span ContextVars SHALL be restored. After 10,000 sequential short invocations, the registry size SHALL return to 0 and memory SHALL not grow linearly with completed span count.

#### Scenario: registry empty after many calls
- **WHEN** 10,000 sequential TASK invocations complete
- **THEN** the Span Event Sink Registry contains zero entries

