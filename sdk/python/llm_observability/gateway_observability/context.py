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
class GatewayContextState:
    """Runtime tuple held in the ContextVar (router slot + attempt slot).

    The two slots are managed independently: closing an Attempt MUST only
    touch the attempt slot; only a Router terminal state may clear the router
    slot (spec: context slot semantics). The attempt slot holds an
    ``ActiveAttemptRef`` (weak) so an ended Attempt never lingers as a stale
    strong reference across threads.
    """
    router: Optional[object] = None
    active_attempt: Optional[ActiveAttemptRef] = None


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
        state = GatewayContext.get()
        if state is None or state.active_attempt is None:
            return None
        return state.active_attempt.attempt()

    @staticmethod
    def get() -> GatewayContextState:
        """Get the current gateway context state (never raises).

        Lazily invalidates the attempt slot: if the active Attempt is dead or
        closed, the current thread's attempt slot is cleared (a per-thread
        ``ContextVar.set``, no foreign token needed) so no stale ended Attempt
        is surfaced. This makes cross-thread cleanup unnecessary — an ended
        Attempt stops appearing the moment any thread reads the context.
        """
        state = _gateway_context_var.get()
        if state is None:
            return GatewayContextState()
        ref = state.active_attempt
        if ref is not None and ref.attempt() is None:
            # Attempt ended (dead referent or _closed). Clear this thread's
            # attempt slot lazily; preserve the router slot.
            try:
                _gateway_context_var.set(
                    GatewayContextState(router=state.router, active_attempt=None)
                )
            except Exception as e:
                logger.error("Gateway lazy attempt-slot clear failed: %s", e)
            return GatewayContextState(router=state.router, active_attempt=None)
        return state

    @staticmethod
    def enter_router(router) -> Tuple[Token, object]:
        """Set the active router. Returns (token, previous_router)."""
        prev = _gateway_context_var.get()
        prev_router = prev.router if prev is not None else None
        token = _gateway_context_var.set(
            GatewayContextState(router=router)
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
        if state is None:
            state = GatewayContextState()
        try:
            ref = ActiveAttemptRef(weakref.ref(attempt))
        except TypeError:
            # Cannot weak-reference (e.g. a bare object) — fall back to a
            # strong-holding ref that still respects _closed on read.
            ref = ActiveAttemptRef(lambda: attempt)
        token = _gateway_context_var.set(
            GatewayContextState(router=state.router, active_attempt=ref)
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
            router = state.router if state is not None else None
            # Clear only the attempt slot, keeping the router slot intact.
            _gateway_context_var.set(
                GatewayContextState(router=router, active_attempt=None)
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
            router = state.router if state is not None else None
            _gateway_context_var.set(
                GatewayContextState(router=router, active_attempt=None)
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
