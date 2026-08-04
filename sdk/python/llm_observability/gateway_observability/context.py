"""Gateway data model and runtime context (spec §8, runtime spec).

GatewayRequestContext — immutable request facts extracted by the adapter.
GatewayContext       — ContextVar holding the active (router, active_attempt).

Context reset is fail-open: a reset failure never changes business behavior.
"""
import logging
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
class GatewayContextState:
    """Runtime tuple held in the ContextVar (router slot + attempt slot).

    The two slots are managed independently: closing an Attempt MUST only
    touch the attempt slot; only a Router terminal state may clear the router
    slot (spec: context slot semantics).
    """
    router: Optional[object] = None
    active_attempt: Optional[object] = None


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
    def get() -> GatewayContextState:
        """Get the current gateway context state (never raises)."""
        state = _gateway_context_var.get()
        if state is None:
            state = GatewayContextState()
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

        Returns a token usable with ``reset_attempt``. Fail-open: if no router
        exists the attempt slot is still recorded on a transient state.
        """
        state = _gateway_context_var.get()
        if state is None:
            state = GatewayContextState()
        token = _gateway_context_var.set(
            GatewayContextState(router=state.router, active_attempt=attempt)
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
