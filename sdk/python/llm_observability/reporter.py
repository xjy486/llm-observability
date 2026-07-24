"""Async telemetry reporter for the SDK.

Principles (matching Proxy reporter):
  - Fail-open: telemetry errors never propagate to business code
  - Async: background task sends batches
  - Bounded queue: drops records when full
  - Batch: sends multiple records per HTTP request
"""
import asyncio
import logging
from collections import deque
from typing import Optional

import aiohttp

logger = logging.getLogger("llm_obs.reporter")


class Reporter:
    """Async telemetry reporter with background batch sending.

    The report() method is synchronous and non-blocking — it just
    enqueues. A background asyncio task periodically flushes the queue
    in batches to the Core ingest endpoint.
    """

    def __init__(
        self,
        endpoint: str,
        max_queue_size: int = 1000,
        batch_size: int = 50,
        flush_interval: float = 5.0,
        timeout: float = 10.0,
    ):
        self.ingest_url = endpoint.rstrip("/") + "/api/v1/ingest"
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
        logger.info("SDK reporter started, endpoint=%s", self.ingest_url)

    async def stop(self):
        """Stop the reporter and flush remaining items."""
        self._stop = True
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush()
        if self._session:
            await self._session.close()
        logger.info(
            "SDK reporter stopped. sent=%d, failed=%d, dropped=%d",
            self._sent_count, self._fail_count, self._dropped_count,
        )

    def report(self, telemetry: dict):
        """Queue a telemetry record. Non-blocking, never raises."""
        if self._stop:
            return
        if len(self._queue) >= self.max_queue_size:
            self._dropped_count += 1
            logger.warning("SDK queue full, dropping record. dropped=%d", self._dropped_count)
            return
        self._queue.append(telemetry)

    async def flush(self):
        """Manually trigger a flush."""
        await self._flush()

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def sent_count(self) -> int:
        return self._sent_count

    @property
    def fail_count(self) -> int:
        return self._fail_count

    async def _flush_loop(self):
        """Background loop that periodically flushes the queue."""
        while not self._stop:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("SDK flush loop error: %s", e)

    async def _flush(self):
        """Send queued records in batches."""
        if not self._queue or not self._session:
            return

        batch = []
        while self._queue and len(batch) < self.batch_size:
            batch.append(self._queue.popleft())

        if not batch:
            return

        try:
            async with self._session.post(
                self.ingest_url,
                json={"records": batch},
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == 200:
                    self._sent_count += len(batch)
                    logger.debug("SDK sent %d records", len(batch))
                else:
                    self._fail_count += len(batch)
                    logger.error("SDK ingest failed: status=%d", resp.status)
                    for item in reversed(batch):
                        if len(self._queue) < self.max_queue_size:
                            self._queue.appendleft(item)
        except Exception as e:
            self._fail_count += len(batch)
            logger.error("SDK report error: %s", e)
            for item in reversed(batch):
                if len(self._queue) < self.max_queue_size:
                    self._queue.appendleft(item)
