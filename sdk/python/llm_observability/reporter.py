"""Async telemetry reporter for the SDK.

Principles (matching Proxy reporter):
  - Fail-open: telemetry errors never propagate to business code
  - Async: background task sends batches
  - Bounded queue: drops records when full
  - Batch: sends multiple records per HTTP request

P0-1: Supports both async (start/stop) and sync (start_sync/stop_sync) lifecycle.
The sync interface is used by Observability.init() to auto-manage the Reporter
in a dedicated background thread with its own asyncio event loop.
"""
import asyncio
import logging
import threading
from collections import deque
from typing import Optional

import aiohttp

logger = logging.getLogger("llm_obs.reporter")


class Reporter:
    """Async telemetry reporter with background batch sending.

    The report() method is synchronous and non-blocking — it just
    enqueues. A background asyncio task periodically flushes the queue
    in batches to the Core ingest endpoint.

    P0-1: Can be started synchronously via start_sync() which creates
    a dedicated background thread with its own event loop. This allows
    the Reporter to work in both sync and async applications without
    requiring the user to manage the event loop.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: Optional[str] = None,
        max_queue_size: int = 1000,
        batch_size: int = 50,
        flush_interval: float = 5.0,
        timeout: float = 10.0,
    ):
        self.ingest_url = endpoint.rstrip("/") + "/api/v1/ingest"
        self.api_key = api_key
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

        # P0-1: Background thread infrastructure for sync lifecycle
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()

    async def start(self):
        """Start the background flush loop (async context)."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("SDK reporter started, endpoint=%s", self.ingest_url)

    async def stop(self):
        """Stop the reporter and flush remaining items (async context)."""
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

    def start_sync(self):
        """Start the Reporter in a dedicated background thread with its own event loop.

        P0-1: This allows Observability.init() to auto-start the Reporter
        without requiring the user to manage asyncio. The background thread
        runs its own event loop where the Reporter's async flush task operates.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("SDK reporter thread already running")
            return

        self._stop = False
        self._ready.clear()

        self._thread = threading.Thread(
            target=self._run_in_thread,
            name="llm-obs-reporter",
            daemon=True,
        )
        self._thread.start()

        # Wait for the loop to be ready
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("Reporter background thread failed to start")

        logger.info("SDK reporter started (sync mode), endpoint=%s", self.ingest_url)

    def stop_sync(self):
        """Stop the Reporter and its background thread.

        P0-1: Flushes remaining items, stops the event loop, and joins the thread.
        Safe to call from any thread.
        """
        if self._loop is None or self._thread is None:
            return

        self._stop = True

        # Schedule stop coroutine on the loop
        if self._loop and not self._loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(self.stop(), self._loop)
            try:
                future.result(timeout=10.0)
            except Exception as e:
                logger.error("Reporter stop error: %s", e)

            # Stop the loop and join thread
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

        self._thread = None
        self._loop = None
        self._ready.clear()
        logger.info("SDK reporter stopped (sync mode)")

    def _run_in_thread(self):
        """Run the Reporter's event loop in a background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _init():
            await self.start()
            self._ready.set()

        self._loop.run_until_complete(_init())
        # Run until stopped externally
        self._loop.run_forever()

        # Cleanup
        if not self._loop.is_closed():
            self._loop.close()

    def report(self, telemetry: dict):
        """Queue a telemetry record. Non-blocking, never raises.

        P1-2: Does not enqueue if _stop is True.
        """
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
        """Send queued records in batches.

        P1-2: Includes Authorization header if api_key is configured.
        """
        if not self._queue or not self._session:
            return

        batch = []
        while self._queue and len(batch) < self.batch_size:
            batch.append(self._queue.popleft())

        if not batch:
            return

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with self._session.post(
                self.ingest_url,
                json={"records": batch},
                headers=headers,
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
