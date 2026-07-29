## ADDED Requirements

### Requirement: Per-invocation isolation
Each root LangChain invocation SHALL create a fresh `LangChainObservabilityCallbackHandler`, `CallbackRunRegistry`, chain event counters, and auto-root state. Nested Runnable calls within the same root invocation SHALL reuse the root's state and SHALL NOT create a second AGENT root. State SHALL NOT leak across invocations.

#### Scenario: Nested calls share root
- **WHEN** a RunnableSequence invokes two child Runnables within one root `invoke`
- **THEN** exactly one AGENT span is created and both children attach to it

#### Scenario: Sequential invocations are isolated
- **WHEN** the same Config is used for two sequential `invoke` calls
- **THEN** each invocation uses a fresh handler and the second invocation's spans do not reference the first's state

### Requirement: Non-destructive user Config
Auto-instrumentation SHALL copy the user-provided Config before merging the callback handler. The original Config object and its callbacks SHALL be unchanged after the call. The same Config SHALL be safe to reuse sequentially and concurrently.

#### Scenario: Original Config unchanged
- **WHEN** a user passes `config={"callbacks": [user_handler]}` to an auto-instrumented `invoke`
- **THEN** after the call, the original config dict still has `callbacks == [user_handler]` and `user_handler` is the same object

#### Scenario: Concurrent reuse
- **WHEN** the same Config is used by two concurrent `ainvoke` calls
- **THEN** neither call mutates the other's callbacks and no handler cross-contamination occurs

### Requirement: User callback preservation
Auto-instrumentation SHALL preserve user callbacks whether passed as `None`, a `list[BaseCallbackHandler]`, a `CallbackManager`, or an `AsyncCallbackManager`. User callbacks SHALL still be called, with unchanged call count and order, and SHALL NOT be replaced or permanently written into the Config.

#### Scenario: User list callback still called
- **WHEN** a user passes a list with a custom callback handler that increments a counter on `on_chain_start`
- **THEN** the counter is incremented the expected number of times after `invoke`

### Requirement: Hard dedup
For each root invocation there SHALL be at most one AGENT span. For each model attempt there SHALL be exactly one LLM span. For each tool attempt there SHALL be exactly one TOOL span. For each provider attempt there SHALL be exactly one GATEWAY span. This SHALL hold for Auto+observe_runnable, Auto+observe_agent, Auto+middleware, Auto+user-callback, and Auto+OpenAI combinations.

#### Scenario: Auto plus OpenAI dedup
- **WHEN** LangChain auto and OpenAI auto are both enabled and a model is called once
- **THEN** exactly one LLM span and one GATEWAY span are produced

### Requirement: Auto-instrumentation default
`auto_instrument_langchain` SHALL default to `False` (opt-in). When the optional LangChain dependency is not installed, enabling it SHALL only emit a warning and SHALL NOT affect business. Documentation SHALL state that `Observability.init(auto_instrument_langchain=True)` is required for auto-observation.

#### Scenario: Missing dependency is warning-only
- **WHEN** `auto_instrument_langchain=True` is set but `langchain_core` is not installed
- **THEN** init completes successfully, a warning is logged, and business code runs uninstrumented
