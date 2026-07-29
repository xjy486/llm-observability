# Phase 2.4 Closeout Fixes

## Why

Closeout review identified missing callback inheritance, event-sink lifecycle leaks, incomplete async context guarantees, ineffective custom-event payload-off semantics, and insufficient Tool/astream E2E coverage.

## What Changes

- Make injected LangChain observability callbacks inheritable without mutating user-owned managers.
- Release Tool, Retriever, and LLM event sinks on all completion, error, and forced-cleanup paths.
- Restore the parent context when callback finalization crosses an async context boundary.
- Preserve controlled Custom Event names and metadata when payload capture is disabled.
- Add targeted regression tests and real Tool/`astream` E2E scenarios.

## Non-goals

- Changing existing span kinds or the public trace data model.
- Introducing a new LangChain integration API.
