# Design: fix-gateway-router-context-weakref

## Context

`GatewayContextState.router` (`context.py:82`) is a strong reference; `get()`
(`context.py:112`) only lazily invalidates the Attempt slot. After a
cross-thread `Router.finalize()` sets `_closed=True`, the owner thread's
`active_router` returns the ended Router and pins `Router._attempts` → all
Attempts in memory (defeating the weak-attempt fix). Separately
`try_aggregate_result` (`attempt_span.py:496`) sets
`_aggregated_to_router=True` and releases `_lifecycle_lock` BEFORE calling
`Router.register_attempt_result` — a `Router.finalize()` in that gap reports the
Router with a pre-publish aggregate, so the reported Record can miss the
result. And `GatewayContextState.active_attempt` now returns an `ActiveAttemptRef`
(public-API regression; `GatewayContextState`/`get_gateway_context` are exported).

Confirmed against current code (line numbers above).

## Goals / Non-Goals

**Goals**
- No stale `active_router` on any thread after the Router ended (cross-thread
  finalize included); no transitive Attempt pinning.
- The Router Record reported to the Reporter always reflects a fully-published
  aggregate (no claim-before-publish).
- Public `GatewayContextState.router`/`.active_attempt` keep returning the live
  object / None (not the weak ref).

**Non-Goals**
- No change to Router enter/exit semantics (still owner-scoped via token).
- No new SpanKind; no business-exception swallowing.

## Decisions

### D1 — Router slot weak + lazy `get` (P0-1)

`GatewayContextState` holds the Router behind a private `_router_ref`
(`ActiveRouterRef(weakref.ref(router))`) and the Attempt behind
`_active_attempt_ref` (renamed from `active_attempt`). Both exposed via
`@property`:

```
@property
def router(self):
    r = self._router_ref.router() if self._router_ref else None
    return None if (r is None or getattr(r, "_closed", False)) else r

@property
def active_attempt(self):
    return self._active_attempt_ref.attempt() if self._active_attempt_ref else None
```

`GatewayContext.get()` invalidates BOTH: if `state.router` is None (dead/closed
Router) OR the Attempt ref is dead/closed, clear the current thread's whole
state (both slots) via a per-thread `ContextVar.set` and return the cleared
state. `Runtime.active_router()` reads via `get().router` → never an ended
Router. With the Router slot weak, the ended Router (and its `_attempts`) can
be collected once no thread holds it; until collection, `get()` hides it.

### D2 — publish-before-claim (P0-2)

```
def try_aggregate_result(self, result) -> bool:
    with self._lifecycle_lock:
        if self._aggregated_to_router:
            return False
        router = self._router
        if router is not None:
            router.register_attempt_result(result)   # under Router _aggregate_lock
        self._aggregated_to_router = True            # claim AFTER publish
        return True
```

`register_attempt_result` is a pure in-memory mutation under `_aggregate_lock`
(no network/Reporter), so holding `_lifecycle_lock` across it is safe and
correct. A `Router.finalize()` that observes `_aggregated_to_router=True` is
now guaranteed the result is already in the aggregate → the reported Record
matches the final in-memory state. Lock ordering: `Attempt._lifecycle_lock` →
`Router._aggregate_lock` (audited — no path reverses it; `register_attempt_result`
takes only `_aggregate_lock`, never `_lifecycle_lock`).

### D3 — public-API-safe properties (P1)

`ActiveAttemptRef` stays; add `ActiveRouterRef`; both fields private
(`_router_ref`, `_active_attempt_ref`); `router`/`active_attempt` are
`@property` dereferencing them. Existing callers (`state.router is X`,
`state.active_attempt`) keep working (return the live object). Callers that
held the raw ref (`attempt_span._cleanup`'s `current.active_attempt.attempt()`,
`runtime.active_attempt`'s `state.active_attempt.attempt()`) are simplified to
use the property directly.

## Risks / Trade-offs

- [Holding `_lifecycle_lock` across `register_attempt_result`] —
  `register_attempt_result` is O(1) dict/counter work under `_aggregate_lock`;
  bounded. The reverse-order hazard is audited absent.
- [weak Router slot + `__del__`] — `weakref.ref(router)` created in
  `enter_router` while the Router is alive; no `__del__` interaction.
- [`get()` clearing the Router slot when only the Attempt ended] — no: a dead
  Attempt clears only the attempt slot (Router still alive); a dead/closed
  Router clears both (an ended Router's attempt is irrelevant). The check is
  `router is None or attempt-dead-or-closed`.
- [property on frozen dataclass] — `@property` on a frozen dataclass works
  (the field is private, not in `__init__` args). Use
  `dataclasses.field(default=None, repr=False)`.

## Migration Plan

1. D1 (weak Router slot + lazy `get` + `active_router` deref) + 4 tests.
2. D2 (publish-before-claim) + 5 tests asserting the REPORTED Record.
3. D3 (properties) + adapt internal callers.
4. Full regression + push; archive after CI green + 0-skipped.

## Open Questions
(none)
