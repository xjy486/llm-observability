"""Tests for the async telemetry Reporter."""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from llm_observability.reporter import Reporter


def test_reporter_never_raises_on_report():
    """report() must never raise, even before start()."""
    r = Reporter(endpoint="http://localhost:99999", max_queue_size=10)
    r.report({"trace_id": "t1", "span_id": "s1"})
    # Should not raise
    r.report({"trace_id": "t2", "span_id": "s2"})


def test_reporter_drops_when_queue_full():
    """Drops records when queue is full, tracks drop count."""
    r = Reporter(endpoint="http://localhost:99999", max_queue_size=2)
    r.report({"span_id": "s1"})
    r.report({"span_id": "s2"})
    r.report({"span_id": "s3"})  # should be dropped
    assert r.dropped_count == 1


def test_reporter_fail_open_on_send_error():
    """Reporter failure does not propagate to caller."""
    r = Reporter(
        endpoint="http://localhost:99999",
        batch_size=1,
        flush_interval=0.1,
    )

    async def main():
        await r.start()
        r.report({"trace_id": "t1", "span_id": "s1", "span_kind": "LLM"})
        await asyncio.sleep(0.3)  # wait for flush attempt
        await r.stop()
        # Reporter should have failed (connection error) but not crash
        assert r.fail_count > 0

    asyncio.run(main())


def test_reporter_batch_send_success():
    """Reporter sends batch to a mock server."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json

    received = []

    class MockHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            data = json.loads(body)
            received.extend(data.get("records", []))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        r = Reporter(
            endpoint=f"http://127.0.0.1:{port}",
            batch_size=10,
            flush_interval=0.1,
        )

        async def main():
            await r.start()
            r.report({"trace_id": "t1", "span_id": "s1", "span_kind": "LLM"})
            r.report({"trace_id": "t1", "span_id": "s2", "span_kind": "LLM"})
            await asyncio.sleep(0.3)
            await r.stop()

        asyncio.run(main())
        assert len(received) == 2
        assert r.sent_count == 2
    finally:
        server.shutdown()
