# Design: fix-gateway-active-attempt-weakref

## Context

`GatewayContextState.active_attempt` (`context.py:55`) is a strong reference.
`clear_attempt(token)` (`context.py:129`) resets only the *calling* thread's
Context; `reset(token)` raises `ValueError` for a foreign (cross-thread) token.
So once an Attempt fully activates on a worker thread and the owner never calls
`close()`, a later cross-thread `force_close` ends the span + clears the
registry but cannot clear the worker's `active_attempt`. The lifecycle-lock +
`_closing` intent flag from `fix-gateway-attempt-lifecycle-lock` only cover
force_close that *begins* during activation; they cannot help when force_close
starts after activation completed.

Separately, the per-Attempt aggregation guard (`if not _aggregated_to_router`)
runs without `_lifecycle_lock` at three sites (`runtime.finalize_attempt`,
streaming `_aggregate_to_router`, `attempt._aggregate_force_close_result`).
The Router `_aggregate_lock` serializes the two calls but both can observe
`_aggregated_to_router=False` and both aggregate — double-counting one Attempt.

Confirmed against current code (line numbers above).

## Goals / Non-Goals

**Goals**
- No stale `active_attempt` on any thread after the Attempt has ended, even
  when the owner never calls `close()` and finalize ran on another thread.
- Exactly-once aggregation of a single Attempt across all paths (finalize /
  streaming / force_close), regardless of which wins.
- `Runtime.active_attempt()` never returns an ended Attempt.

**Non-Goals**
- No change to Router-slot lifecycle (still owner-managed via enter/exit).
- No new SpanKind; no business-exception swallowing; no per-thread registries.
- Asyncio-Context (single-thread, copied Context) already worked via the
  cross-Context `clear_attempt_only` path; this change unifies it.

## Decisions

### D1 — Weakref active-attempt with lazy invalidation (Blocker)

Wrap the attempt slot in a small ref holder:

```
@dataclass(frozen=True)
class ActiveAttemptRef:
    ref: "weakref.ref"   # weakref to the AttemptSpan
```

`GatewayContextState.active_attempt` becomes `Optional[ActiveAttemptRef]`.
`set_attempt(attempt)` stores `ActiveAttemptRef(weakref.ref(attempt))`.
`GatewayContext.get()` dereferences: `a = ref(); if a is None or a._closed:
<clear this thread's attempt slot>; return None`. The clear is a per-thread
`ContextVar.set` (no foreign token needed) — safe on whichever thread reads.
The strong reference is gone, so an ended Attempt with no other refs can be
collected; even before collection, `get()` hides it via `._closed`.

`Runtime.active_attempt()` / `active_router()` read through `get()` and thus
never surface an ended Attempt. The owner's `close()` still clears its own
token (fast path); the weakref path is the safety net for the no-close case.

**Alternative:** freeze the contract to "owner must clear via finally". Rejected
— weakens the Phase 3 "no ContextVar leak after any terminal path" requirement
and the report flags thread-pool reuse as the realistic failure.

**Why not `weakref.WeakValueDictionary`?** Overkill; a single `weakref.ref`
per state is simpler and the lazy deref in `get()` is the whole mechanism.

### D2 — `try_aggregate_result` single funnel (P1)

```
def try_aggregate_result(self, result) -> bool:
    with self._lifecycle_lock:
        if self._aggregated_to_router:
            return False
        self._aggregated_to_router = True
        router = self._router
    if router is not None:
        router.register_attempt_result(result)   # under Router _aggregate_lock
    return True
```

The check-and-set is atomic under `_lifecycle_lock` (RLock —
`_aggregate_force_close_result` already runs under it via `force_close`; the
other two callers acquire it fresh). `register_attempt_result` is called
*outside* the Attempt lock (it takes the Router `_aggregate_lock`) to avoid
holding `_lifecycle_lock` across Router aggregation. Only the first caller
returns True; everyone else returns False and does nothing → exactly-once.

Route the three sites through it:
- `runtime.finalize_attempt`: `attempt.try_aggregate_result(result)`.
- streaming `_aggregate_to_router`: `self._attempt.try_aggregate_result(result)`.
- `attempt._aggregate_force_close_result`: inline the guarded check-and-set
  (already under `_lifecycle_lock` from `force_close`) — or call
  `try_aggregate_result` (re-entrant RLock makes this safe).

## Risks / Trade-offs

- [weakref dead before owner reads] — `get()` returns None and clears the
  slot; the Router slot is unaffected. Safe.
- [`_closed` is a plain bool read under no lock in `get()`] — a torn read at
  worst yields one stale `get()` that is corrected on the next read; the
  strong-ref leak is gone either way. Acceptable.
- [holding `_lifecycle_lock` across `register_attempt_result`] — avoided by
  releasing after the check-and-set; `_aggregate_lock` is a separate
  per-Router lock, no nesting with `_lifecycle_lock` (ordering: lifecycle →
  aggregate, never reversed).
- [weakref + `__del__` ordering] — `weakref.ref(attempt)` is created in
  `set_attempt` while the Attempt is alive; no `__del__` interaction.

## Migration Plan

1. D1 (weakref state + lazy `get` + `set_attempt`/`clear_attempt`) + 3 tests.
2. D2 (`try_aggregate_result` + route 3 sites) + 3 tests.
3. Full regression + push; archive after CI green + 0-skipped.

## Open Questions
(none)
