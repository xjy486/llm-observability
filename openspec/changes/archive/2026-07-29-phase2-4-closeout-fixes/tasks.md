# Tasks

- [x] Add regression coverage for inheritable `CallbackManager` model, Tool, and Retriever callbacks.
- [x] Verify repeated invokes do not accumulate observability handlers.
- [x] Add Tool/Retriever/LLM event-sink cleanup tests for success, error, forced cleanup, and span-end failure.
- [x] Add async context restoration tests for LLM callbacks, parallel branches, consecutive `ainvoke`, and early `astream` close.
- [x] Preserve controlled Custom Event names when `payload_strategy=off`.
- [x] Fix CallbackManager inheritance and all event-sink cleanup paths.
- [x] Make real E2E Scenario 5 execute a Tool and add real `astream` coverage.
- [x] Run targeted and SDK-wide regression tests in `.venv`.
- [x] Fix `_unregister_span` to also remove local `_spans_by_id` references on Tool/Retriever/LLM end, error, forced cleanup, and span-end failure.
- [x] Add regression coverage for local span reference cleanup and manual handler reuse bounded map.
- [x] Add real E2E nested TOOL→LLM→GATEWAY scenario (Scenario 5b).
- [x] Clarify E2E Scenario 5 as sibling TOOL+LLM and assert LLM parent is AGENT.
