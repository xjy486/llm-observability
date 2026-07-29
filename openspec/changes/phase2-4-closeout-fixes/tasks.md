# Tasks

- [x] Add regression coverage for inheritable `CallbackManager` model, Tool, and Retriever callbacks.
- [x] Verify repeated invokes do not accumulate observability handlers.
- [x] Add Tool/Retriever/LLM event-sink cleanup tests for success, error, forced cleanup, and span-end failure.
- [x] Add async context restoration tests for LLM callbacks, parallel branches, consecutive `ainvoke`, and early `astream` close.
- [x] Preserve controlled Custom Event names when `payload_strategy=off`.
- [x] Fix CallbackManager inheritance and all event-sink cleanup paths.
- [x] Make real E2E Scenario 5 execute a Tool and add real `astream` coverage.
- [x] Run targeted and SDK-wide regression tests in `.venv`.
