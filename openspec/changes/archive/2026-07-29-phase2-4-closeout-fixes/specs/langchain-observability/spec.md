# LangChain Observability Closeout

## MODIFIED Requirements

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
