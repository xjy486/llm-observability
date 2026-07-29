# Design

The Runnable wrapper clones a supplied `CallbackManager`, then adds the fresh observability handler with inheritance enabled. The original manager remains untouched.

Callback-created spans register an event sink keyed by `(trace_id, span_id)`. Every normal, error, and forced cleanup path closes the span and unregisters the sink in `finally` blocks. LLM callback state retains its parent context so a failed cross-context token reset can restore the parent context in the current task.

Regression tests exercise real LangChain callback objects, fake chat/retriever/tool runnables, parallel branches, async invocation, early async-stream close, payload-off custom events, and sink cleanup. The real E2E runner executes a nested Tool and an async stream against the proxy/core stack.
