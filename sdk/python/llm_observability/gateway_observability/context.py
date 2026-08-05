"""Gateway data model and runtime context (spec §8, runtime spec).

GatewayRequestContext — immutable request facts extracted by the adapter.
GatewayContext       — ContextVar holding the active (router, active_attempt).

Context reset is fail-open: a reset failure never changes business behavior.
"""
import logging
import weakref
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Optional, Tuple

logger = logging.getLogger("llm_obs.gateway.context")


@dataclass(frozen=True)
class GatewayRequestContext:
    """Gateway request facts (spec §8.1).

    Attributes:
        gateway_name: Gateway instance name.
        gateway_version: Optional gateway software version.
        request_id: Gateway-internal request ID.
        protocol: Relay/protocol mode (e.g. 'openai-compatible', 'anthropic').
        route: Route path / endpoint (e.g. '/v1/chat/completions').
        requested_model: Model name requested by the caller.
        user_id: Optional association user ID.
        session_id: Optional association session ID.
        message_id: Optional association message ID.
        app_name: Optional association app name.
        business_scenario: Optional association business scenario.
    """
    gateway_name: str = "unknown"
    gateway_version: Optional[str] = None
    request_id: Optional[str] = None
    protocol: str = "openai-compatible"
    route: str = ""
    requested_model: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    app_name: Optional[str] = None
    business_scenario: Optional[str] = None


@dataclass(frozen=True)
class ActiveAttemptRef:
    """Weak holder for the active Attempt in a GatewayContextState.

    Python ContextVars are per-Context/per-thread: a finalize running on another
    thread cannot reset a worker thread's ``active_attempt`` via the saved token.
    Storing a weak reference (instead of a strong one) lets the slot be lazily
    invalidated on read — an ended Attempt (dead referent, or ``_closed``) is
    hidden the moment any thread reads it, without cross-thread token reset.
    """
    ref: "weakref.ref"

    def attempt(self):
        """Return the live Attempt, or None if dead/closed."""
        try:
            a = self.ref()
        except Exception:
            return None
        if a is None:
            return None
        if getattr(a, "_closed", False):
            return None
        return a


@dataclass(frozen=True)
class ActiveRouterRef:
    """Weak holder for the active Router in a GatewayContextState.

    Same rationale as ``ActiveAttemptRef``: a cross-thread ``Router.finalize()``
    cannot reset the owner thread's Router slot, so the slot holds a weak
    reference and is lazily invalidated on read when the Router is dead or
    ``_closed``. Without this the strong Router slot would transitively pin
    ``Router._attempts`` (all Attempts) in memory.
    """
    ref: "weakref.ref"

    def router(self):
        """Return the live Router, or None if dead/closed."""
        try:
            r = self.ref()
        except Exception:
            return None
        if r is None:
            return None
        if getattr(r, "_closed", False):
            return None
        return r


def _weak_router_ref(router):
    try:
        return ActiveRouterRef(weakref.ref(router))
    except TypeError:
        return ActiveRouterRef(lambda: router)


def _weak_attempt_ref(attempt):
    try:
        return ActiveAttemptRef(weakref.ref(attempt))
    except TypeError:
        return ActiveAttemptRef(lambda: attempt)


@dataclass
class GatewayContextState:
    """Runtime tuple held in the ContextVar (router slot + attempt slot).

    Both slots hold WEAK references (``_router_ref`` / ``_active_attempt_ref``)
    so an ended Router/Attempt is lazily invalidated on read by ``GatewayContext.get()``
    — a cross-thread finalize cannot reset the owner's ContextVar, so the weak
    slot + lazy deref is the only leak-free path. The public ``router`` /
    ``active_attempt`` accessors dereference to the live object (or None),
    preserving the historical public API contract.
    """
    _router_ref: Optional[ActiveRouterRef] = field(default=None, repr=False)
    _active_attempt_ref: Optional[ActiveAttemptRef] = field(default=None, repr=False)

    @property
    def router(self):
        """The live active Router, or None if dead/closed."""
        if self._router_ref is None:
            return None
        return self._router_ref.router()

    @property
    def active_attempt(self):
        """The live active Attempt, or None if dead/closed."""
        if self._active_attempt_ref is None:
            return None
        return self._active_attempt_ref.attempt()


_gateway_context_var: ContextVar[Optional[GatewayContextState]] = ContextVar(
    "llm_obs_gateway_context", default=None
)


class GatewayContext:
    """ContextVar-backed runtime context holding (router, active_attempt).

    ``enter_router``/``exit_router`` manage the router slot; ``set_attempt``/
    ``clear_attempt`` manage the active-attempt slot. All mutations return
    tokens so callers can restore the previous state.
    """

    @staticmethod
    def active_attempt():
        """Return the live active Attempt on the current thread, or None.

        Convenience reader that dereferences the weak slot (and thus returns
        None for any ended Attempt, lazily clearing the slot via ``get()``).
        """
        return GatewayContext.get().active_attempt

    @staticmethod
    def get() -> GatewayContextState:
        """Get the current gateway context state (never raises).

        Lazily invalidates BOTH slots: if the active Router is dead/closed OR
        the active Attempt is dead/closed, the current thread's whole context
        is cleared (a per-thread ``ContextVar.set``, no foreign token needed)
        so no stale ended Router/Attempt is surfaced. This makes cross-thread
        cleanup unnecessary — an ended Router/Attempt stops appearing the
        moment any thread reads the context, and the weak Router slot no
        longer transitively pins ``Router._attempts``.
        """
        state = _gateway_context_var.get()
        if state is None:
            return GatewayContextState()
        # A dead/closed Router clears the whole context (its attempt is moot).
        router_ref = state._router_ref
        if router_ref is not None and router_ref.router() is None:
            cleared = GatewayContextState()
            try:
                _gateway_context_var.set(cleared)
            except Exception as e:
                logger.error("Gateway lazy router-slot clear failed: %s", e)
            return cleared
        # Router alive — check the attempt slot independently.
        attempt_ref = state._active_attempt_ref
        if attempt_ref is not None and attempt_ref.attempt() is None:
            cleared = GatewayContextState(_router_ref=router_ref)
            try:
                _gateway_context_var.set(cleared)
            except Exception as e:
                logger.error("Gateway lazy attempt-slot clear failed: %s", e)
            return cleared
        return state

    @staticmethod
    def enter_router(router) -> Tuple[Token, object]:
        """Set the active router (weakly). Returns (token, previous_router)."""
        prev = _gateway_context_var.get()
        prev_router = prev.router if prev is not None else None
        token = _gateway_context_var.set(
            GatewayContextState(_router_ref=_weak_router_ref(router))
        )
        return token, prev_router

    @staticmethod
    def exit_router(token: Token) -> object:
        """Restore the router context. Returns the router that was active (if any).

        Fail-open: a reset failure is logged and never propagates. When the
        token belongs to a different asyncio Context (streaming task runs in
        its own context copy), the current context is cleared instead so no
        stale Router remains.
        """
        router = None
        try:
            state = _gateway_context_var.get()
            router = state.router if state is not None else None
            _gateway_context_var.reset(token)
        except ValueError:
            # Cross-Context token — clear the current context's router slot.
            try:
                _gateway_context_var.set(GatewayContextState())
            except Exception as e:
                logger.error("Gateway context cross-context clear failed: %s", e)
        except Exception as e:
            logger.error("Gateway context reset failed (router): %s", e)
        return router

    @staticmethod
    def set_attempt(attempt) -> Token:
        """Attach the active attempt to the current router state.

        Stores a weak reference so an ended Attempt is lazily hidden by ``get()``
        without cross-thread token reset. Returns a token usable with
        ``clear_attempt``. Fail-open: if no router exists the attempt slot is
        still recorded on a transient state.
        """
        state = _gateway_context_var.get()
        router_ref = state._router_ref if state is not None else None
        token = _gateway_context_var.set(
            GatewayContextState(_router_ref=router_ref,
                                 _active_attempt_ref=_weak_attempt_ref(attempt))
        )
        return token

    @staticmethod
    def clear_attempt(token: Token):
        """Detach the active attempt — clears ONLY the attempt slot.

        The router slot is always preserved: attempt close (normal, error,
        async, or cross-context reset) must never clear the Router context.
        Fail-open on reset failure.
        """
        try:
            state = _gateway_context_var.get()
            router_ref = state._router_ref if state is not None else None
            # Clear only the attempt slot, keeping the router slot intact.
            _gateway_context_var.set(
                GatewayContextState(_router_ref=router_ref)
            )
            try:
                _gateway_context_var.reset(token)
            except ValueError:
                # Cross-Context token — the slot clear above already applied.
                pass
        except Exception as e:
            logger.error("Gateway attempt clear failed: %s", e)

    @staticmethod
    def clear_attempt_only():
        """Clear only the attempt slot in the current context (no token).

        Used when the reset token belongs to another asyncio Context. The
        router slot is always preserved. Fail-open.
        """
        try:
            state = _gateway_context_var.get()
            router_ref = state._router_ref if state is not None else None
            _gateway_context_var.set(
                GatewayContextState(_router_ref=router_ref)
            )
        except Exception as e:
            logger.error("Gateway attempt-only clear failed: %s", e)

    @staticmethod
    def clear():
        """Reset the gateway context to None (fail-open)."""
        try:
            _gateway_context_var.set(None)
        except Exception as e:
            logger.error("Gateway context clear failed: %s", e)


def clear_gateway_context():
    """Force-clear the whole gateway context (both slots).

    Router-terminal-state use only — Attempt cleanup must go through
    ``GatewayContext.clear_attempt`` so the Router slot survives.
    """
    GatewayContext.clear()


def get_gateway_context() -> GatewayContextState:
    """Convenience: current gateway context state (never raises)."""
    return GatewayContext.get()


def set_gateway_context(router) -> Token:
    """Convenience: enter a router context. Returns a reset token."""
    token, _ = GatewayContext.enter_router(router)
    return token


def reset_gateway_context(token: Token) -> object:
    """Convenience: restore the previous gateway context. Fail-open."""
    return GatewayContext.exit_router(token)


def reset_gateway_context_fail_open(token: Token):
    """Restore the previous gateway context without raising.

    Used on every terminal path (success, error, cancel, close, aclose,
    GeneratorExit) so no stale ContextVar remains.
    """
    try:
        if token is not None:
            GatewayContext.exit_router(token)
    except Exception as e:
        logger.error("Gateway context fail-open reset failed: %s", e)
