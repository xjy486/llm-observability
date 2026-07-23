"""
Async telemetry reporter.

Reports trace/span data to the Observability Core via HTTP.
Uses a background queue + batch sending to avoid blocking the main LLM request.
Never raises — telemetry failures must not affect the main request path.
"""
import asyncio
import json
import time
import logging
from typing import Optional
from collections import deque

import aiohttp

logger = logging.getLogger("proxy.reporter")


class TelemetryReporter:
    """Async telemetry reporter with background queue."""

    def __init__(
        self,
        endpoint: str,
        max_queue_size: int = 1000,
        batch_size: int = 50,
        flush_interval: float = 5.0,
        timeout: float = 10.0,
    ):
        self.endpoint = endpoint.rstrip("/") + "/api/v1/ingest"
        self.max_queue_size = max_queue_size
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.timeout = timeout
        self._queue: deque = deque()
        self._session: Optional[aiohttp.ClientSession] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._stop = False
        self._dropped_count = 0
        self._sent_count = 0
        self._fail_count = 0

    async def start(self):
        """Start the background flush loop."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("Telemetry reporter started, endpoint=%s", self.endpoint)

    async def stop(self):
        """Stop the reporter and flush remaining items."""
        self._stop = True
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        # Final flush
        await self._flush()
        if self._session:
            await self._session.close()
        logger.info(
            "Telemetry reporter stopped. sent=%d, failed=%d, dropped=%d",
            self._sent_count, self._fail_count, self._dropped_count,
        )

    def report(self, telemetry: dict):
        """Queue a telemetry record for async reporting.

        Non-blocking, never raises. If queue is full, drops the record.
        """
        if self._stop:
            return
        if len(self._queue) >= self.max_queue_size:
            self._dropped_count += 1
            logger.warning("Telemetry queue full, dropping record. dropped=%d", self._dropped_count)
            return
        self._queue.append(telemetry)

    async def _flush_loop(self):
        """Background loop that periodically flushes the queue."""
        while not self._stop:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Flush loop error: %s", e)

    async def _flush(self):
        """Send queued telemetry records in batches."""
        if not self._queue or not self._session:
            return

        batch = []
        while self._queue and len(batch) < self.batch_size:
            batch.append(self._queue.popleft())

        if not batch:
            return

        try:
            async with self._session.post(
                self.endpoint,
                json={"records": batch},
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == 200:
                    self._sent_count += len(batch)
                    logger.debug("Sent %d telemetry records", len(batch))
                else:
                    self._fail_count += len(batch)
                    logger.error("Telemetry ingest failed: status=%d", resp.status)
                    # Re-queue on failure (best effort)
                    for item in reversed(batch):
                        if len(self._queue) < self.max_queue_size:
                            self._queue.appendleft(item)
        except Exception as e:
            self._fail_count += len(batch)
            logger.error("Telemetry report error: %s", e)
            # Re-queue on failure
            for item in reversed(batch):
                if len(self._queue) < self.max_queue_size:
                    self._queue.appendleft(item)

    def get_stats(self) -> dict:
        """Get reporter statistics."""
        return {
            "queue_size": len(self._queue),
            "dropped_count": self._dropped_count,
            "sent_count": self._sent_count,
            "fail_count": self._fail_count,
        }
