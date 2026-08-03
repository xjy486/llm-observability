"""Registry — per-request Router/Attempt tracking with guaranteed cleanup.

RouterRegistry / AttemptRegistry key spans by (request_id, span_id). Every
terminal path (success, error, cancel, close, aclose, span-end failure) must
clean entries; the runtime calls cleanup in ``finally`` so nothing leaks.
"""
import logging
import threading
from typing import Optional

logger = logging.getLogger("llm_obs.gateway.registry")


def _safe_str(value) -> str:
    try:
        return str(value)
    except Exception:
        return "unknown"


class GatewayRegistry:
    """Thread-safe per-request registry for Router/Attempt spans.

    Entries are keyed by (request_id, span_id). ``pop``/``clear`` never raise.
    """

    def __init__(self, kind: str = "router"):
        self._kind = kind
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], object] = {}

    def register(self, request_id: Optional[str], span_id: Optional[str], value: object) -> bool:
        try:
            with self._lock:
                self._entries[(_safe_str(request_id), _safe_str(span_id))] = value
            return True
        except Exception as e:
            logger.error("%s registry register failed: %s", self._kind, e)
            return False

    def get(self, request_id: Optional[str], span_id: Optional[str]) -> Optional[object]:
        try:
            with self._lock:
                return self._entries.get((_safe_str(request_id), _safe_str(span_id)))
        except Exception:
            return None

    def remove(self, request_id: Optional[str], span_id: Optional[str]) -> bool:
        try:
            with self._lock:
                self._entries.pop((_safe_str(request_id), _safe_str(span_id)), None)
            return True
        except Exception as e:
            logger.error("%s registry remove failed: %s", self._kind, e)
            return False

    def size(self) -> int:
        try:
            with self._lock:
                return len(self._entries)
        except Exception:
            return 0

    def clear(self) -> int:
        """Clear all entries. Returns the number removed (fail-open)."""
        try:
            with self._lock:
                n = len(self._entries)
                self._entries.clear()
                return n
        except Exception as e:
            logger.error("%s registry clear failed: %s", self._kind, e)
            return 0

    @property
    def kind(self) -> str:
        return self._kind


class RouterRegistry(GatewayRegistry):
    def __init__(self):
        super().__init__(kind="router")


class AttemptRegistry(GatewayRegistry):
    def __init__(self):
        super().__init__(kind="attempt")
