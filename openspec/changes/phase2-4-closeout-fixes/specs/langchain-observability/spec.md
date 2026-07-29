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

### Requirement: Callback-created event sinks have bounded lifetimes

Tool, Retriever, and LLM callback spans MUST unregister their global event sinks after success, error, span-end failure, or forced handler cleanup.

#### Scenario: Normal and error cleanup

- **WHEN** a Tool or Retriever callback completes or errors
- **THEN** its event sink is absent from the global sink registry

#### Scenario: Forced cleanup and LLM end failure

- **WHEN** a handler closes an unfinished run or an LLM span end raises
- **THEN** the corresponding event sink is still removed

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
