"""Bounded accumulation for Runnable streaming output."""
import json
from typing import Any

from ...tool import safe_serialize

DEFAULT_STREAM_BUDGET_BYTES = 64 * 1024


class BoundedStreamAccumulator:
    """Retain stream output without allowing memory to grow unboundedly."""

    def __init__(self, max_bytes: int = DEFAULT_STREAM_BUDGET_BYTES):
        self.max_bytes = max(0, int(max_bytes))
        self._chunks: list[Any] = []
        self._bytes = 0
        self._count = 0
        self._truncated = False
        self._last = None

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def count(self) -> int:
        return self._count

    def append(self, chunk: Any):
        self._count += 1
        self._last = chunk
        if self._truncated:
            return
        try:
            encoded = json.dumps(safe_serialize(chunk), ensure_ascii=False, default=str).encode("utf-8")
            size = len(encoded)
        except Exception:
            size = self.max_bytes + 1
        if self._bytes + size > self.max_bytes:
            self._truncated = True
            return
        self._chunks.append(chunk)
        self._bytes += size

    def finalize(self) -> Any:
        if not self._chunks:
            result = self._last
        elif len(self._chunks) == 1:
            result = self._chunks[0]
        else:
            result = list(self._chunks)
        if self._truncated:
            return {
                "chunks": result if isinstance(result, list) else ([] if result is None else [result]),
                "truncated": True,
                "chunk_count": self._count,
            }
        return result
