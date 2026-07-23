"""
Main proxy handler — transparent LLM API proxy with telemetry capture.

Handles:
- Non-streaming /v1/chat/completions
- Streaming SSE /v1/chat/completions
- W3C Trace Context propagation
- TTFT measurement for streaming
- Token / usage extraction
- Error capture
- Payload capture with masking strategies
"""
import asyncio
import json
import time
import logging
import uuid
from typing import Optional

import aiohttp
from aiohttp import web

from config import ProxyConfig
from trace_context import resolve_trace_context, extract_metadata_headers
from payload import process_payload, extract_request_metadata, extract_response_metadata
from reporter import TelemetryReporter

logger = logging.getLogger("proxy.handler")


class ProxyHandler:
    """Handles proxy requests with telemetry capture."""

    def __init__(self, config: ProxyConfig, reporter: TelemetryReporter):
        self.config = config
        self.reporter = reporter
        self._upstream_session: Optional[aiohttp.ClientSession] = None

    async def setup(self):
        """Initialize upstream HTTP session."""
        self._upstream_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.upstream_timeout),
        )

    async def cleanup(self):
        """Clean up resources."""
        if self._upstream_session:
            await self._upstream_session.close()

    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        """Main entry point — proxy all requests."""
        path = request.path
        method = request.method

        # Check if this path should be observed
        should_observe = any(
            path == p or path.startswith(p + "/") for p in self.config.observed_paths
        )

        if should_observe and method == "POST":
            return await self._handle_observed(request)
        else:
            # Pass through without observation
            return await self._handle_passthrough(request)

    async def _handle_passthrough(self, request: web.Request) -> web.Response:
        """Pass through requests without telemetry capture."""
        if not self._upstream_session:
            return web.Response(status=503, text="Proxy not ready")

        url = self.config.upstream_url + request.path
        if request.query_string:
            url += "?" + request.query_string

        # Forward request
        headers = self._forward_headers(request)
        body = await request.read()

        async with self._upstream_session.request(
            method=request.method,
            url=url,
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as upstream_resp:
            resp_body = await upstream_resp.read()
            return web.Response(
                status=upstream_resp.status,
                body=resp_body,
                headers=dict(upstream_resp.headers),
            )

    async def _handle_observed(self, request: web.Request) -> web.StreamResponse:
        """Handle observed LLM API requests with full telemetry."""
        start_time = time.perf_counter()
        start_wall = time.time()

        # Resolve trace context
        headers_dict = {k: v for k, v in request.headers.items()}
        trace_ctx = resolve_trace_context(headers_dict)
        metadata = extract_metadata_headers(headers_dict)

        # Read request body
        request_body_raw = await request.read()
        try:
            request_body = json.loads(request_body_raw) if request_body_raw else {}
        except json.JSONDecodeError:
            request_body = {}

        is_stream = request_body.get("stream", False)
        request_meta = extract_request_metadata(request_body)

        # Process payload for telemetry
        processed_payload = process_payload(request_body, self.config.payload_strategy, self.config)

        # Sampling check
        should_sample = self._should_sample(is_error=False)

        # Forward to upstream
        upstream_url = self.config.upstream_url + request.path
        if request.query_string:
            upstream_url += "?" + request.query_string

        forward_headers = self._forward_headers(request, preserve_traceparent=False)

        # Send to upstream
        try:
            upstream_resp = await self._upstream_session.post(  # type: ignore[union-attr]
                upstream_url,
                headers=forward_headers,
                data=request_body_raw,
                allow_redirects=False,
            )
        except aiohttp.ClientError as e:
            # Upstream error — capture and return error
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            await self._report_telemetry(
                trace_ctx=trace_ctx,
                metadata=metadata,
                request_meta=request_meta,
                start_wall=start_wall,
                elapsed_ms=elapsed_ms,
                status="ERROR",
                http_status=502,
                error_type="upstream_connection_error",
                error_message=str(e),
                is_stream=is_stream,
                processed_payload=processed_payload if should_sample else None,
                response_payload=None,
                ttft_ms=None,
                stream_chunks=None,
            )
            return web.Response(
                status=502,
                text=json.dumps({"error": {"message": f"Upstream connection error: {e}", "type": "proxy_error"}}),
                content_type="application/json",
            )

        # Handle response based on streaming or not
        if is_stream and upstream_resp.status == 200:
            return await self._handle_streaming_response(
                request, upstream_resp, trace_ctx, metadata, request_meta,
                start_time, start_wall, processed_payload, should_sample,
            )
        else:
            return await self._handle_nonstreaming_response(
                upstream_resp, trace_ctx, metadata, request_meta,
                start_time, start_wall, processed_payload, should_sample, is_stream,
            )

    async def _handle_streaming_response(
        self,
        request: web.Request,
        upstream_resp: aiohttp.ClientResponse,
        trace_ctx,
        metadata,
        request_meta,
        start_time: float,
        start_wall: float,
        processed_payload,
        should_sample: bool,
    ) -> web.StreamResponse:
        """Handle SSE streaming response with TTFT measurement."""
        # Create streaming response
        response = web.StreamResponse(
            status=upstream_resp.status,
            headers=self._filter_response_headers(upstream_resp.headers),
        )
        await response.prepare(request)

        ttft_ms = None
        stream_chunks = []
        first_chunk_time = None
        total_output = b""

        try:
            async for line in upstream_resp.content:
                # Forward to client immediately
                await response.write(line)
                total_output += line

                # Parse SSE data for telemetry
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                        stream_chunks.append(chunk)
                        if first_chunk_time is None:
                            first_chunk_time = time.perf_counter()
                            ttft_ms = (first_chunk_time - start_time) * 1000
                    except json.JSONDecodeError:
                        pass

            await response.write_eof()

        except (ConnectionResetError, asyncio.TimeoutError) as e:
            logger.warning("Streaming interrupted: %s", e)
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            await self._report_telemetry(
                trace_ctx=trace_ctx, metadata=metadata, request_meta=request_meta,
                start_wall=start_wall, elapsed_ms=elapsed_ms,
                status="ERROR", http_status=upstream_resp.status,
                error_type="stream_interrupted", error_message=str(e),
                is_stream=True, processed_payload=processed_payload if should_sample else None,
                response_payload=None, ttft_ms=ttft_ms, stream_chunks=stream_chunks,
            )
            if not response.prepared:
                return web.Response(status=502, text="Streaming error")
            return response

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        is_error = upstream_resp.status >= 400

        # Extract response metadata from streaming chunks
        response_meta = extract_response_metadata(upstream_resp.status, None, stream_chunks)

        # Process response payload
        response_payload = None
        if should_sample and self.config.payload_strategy != "off":
            if self.config.payload_strategy == "metadata_only":
                response_payload = response_meta
            else:
                response_payload = {
                    "stream_chunks_summary": {
                        "chunk_count": len(stream_chunks),
                    },
                    "usage": stream_chunks[-1].get("usage") if stream_chunks else None,
                    "model": stream_chunks[0].get("model") if stream_chunks else None,
                }
                if self.config.payload_strategy == "masked":
                    response_payload = process_payload(response_payload, "masked", self.config)

        status = "ERROR" if is_error else "OK"

        await self._report_telemetry(
            trace_ctx=trace_ctx, metadata=metadata, request_meta=request_meta,
            start_wall=start_wall, elapsed_ms=elapsed_ms,
            status=status, http_status=upstream_resp.status,
            error_type=None if not is_error else "http_error",
            error_message=None if not is_error else f"HTTP {upstream_resp.status}",
            is_stream=True, processed_payload=processed_payload if should_sample else None,
            response_payload=response_payload, ttft_ms=ttft_ms,
            stream_chunks=stream_chunks if should_sample else None,
        )

        return response

    async def _handle_nonstreaming_response(
        self,
        upstream_resp: aiohttp.ClientResponse,
        trace_ctx,
        metadata,
        request_meta,
        start_time: float,
        start_wall: float,
        processed_payload,
        should_sample: bool,
        is_stream: bool,
    ) -> web.Response:
        """Handle non-streaming response."""
        resp_body_raw = await upstream_resp.read()

        try:
            resp_body = json.loads(resp_body_raw) if resp_body_raw else {}
        except json.JSONDecodeError:
            resp_body = {}

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        is_error = upstream_resp.status >= 400

        # For non-streaming, TTFT is effectively the total response time
        ttft_ms = elapsed_ms if not is_stream and not is_error else None

        # Extract response metadata
        response_meta = extract_response_metadata(upstream_resp.status, resp_body, [])

        # Process response payload
        response_payload = None
        if should_sample and self.config.payload_strategy != "off":
            response_payload = process_payload(resp_body, self.config.payload_strategy, self.config)

        # Extract error info
        error_type = None
        error_message = None
        if is_error:
            err_obj = resp_body.get("error", {})
            error_type = err_obj.get("type", "http_error") if isinstance(err_obj, dict) else "http_error"
            error_message = err_obj.get("message", f"HTTP {upstream_resp.status}") if isinstance(err_obj, dict) else f"HTTP {upstream_resp.status}"

        status = "ERROR" if is_error else "OK"

        await self._report_telemetry(
            trace_ctx=trace_ctx, metadata=metadata, request_meta=request_meta,
            start_wall=start_wall, elapsed_ms=elapsed_ms,
            status=status, http_status=upstream_resp.status,
            error_type=error_type, error_message=error_message,
            is_stream=is_stream, processed_payload=processed_payload if should_sample else None,
            response_payload=response_payload, ttft_ms=ttft_ms,
            stream_chunks=None,
        )

        return web.Response(
            status=upstream_resp.status,
            body=resp_body_raw,
            headers=self._filter_response_headers(upstream_resp.headers),
        )

    def _should_sample(self, is_error: bool) -> bool:
        """Determine if this request should be sampled."""
        if is_error and self.config.error_always_capture:
            return True
        import random
        return random.random() < self.config.sample_rate

    async def _report_telemetry(
        self,
        trace_ctx,
        metadata,
        request_meta,
        start_wall: float,
        elapsed_ms: float,
        status: str,
        http_status: int,
        error_type: Optional[str],
        error_message: Optional[str],
        is_stream: bool,
        processed_payload: Optional[dict],
        response_payload: Optional[dict],
        ttft_ms: Optional[float],
        stream_chunks: Optional[list],
    ):
        """Build and queue telemetry record."""
        # Build attributes (OpenTelemetry GenAI semantic conventions)
        attributes = {
            "gen_ai.request.model": request_meta.get("model", "unknown"),
            "gen_ai.operation.name": "chat",
            "llm.stream": is_stream,
            "llm.gateway.name": "one-api-proxy",
            "http.status_code": http_status,
        }

        # Add response model
        if "response_model" in (response_payload or {}):
            attributes["gen_ai.response.model"] = response_payload["response_model"]
        elif "response_model" in request_meta:
            pass

        # Tokens
        resp_meta = response_payload if isinstance(response_payload, dict) else {}
        input_tokens = resp_meta.get("input_tokens") if resp_meta else None
        output_tokens = resp_meta.get("output_tokens") if resp_meta else None
        total_tokens = resp_meta.get("total_tokens") if resp_meta else None

        if not input_tokens and isinstance(resp_meta, dict):
            usage = resp_meta.get("usage", {})
            if isinstance(usage, dict):
                input_tokens = usage.get("prompt_tokens")
        if not output_tokens and isinstance(resp_meta, dict):
            usage = resp_meta.get("usage", {})
            if isinstance(usage, dict):
                output_tokens = usage.get("completion_tokens")
        if not total_tokens and isinstance(resp_meta, dict):
            usage = resp_meta.get("usage", {})
            if isinstance(usage, dict):
                total_tokens = usage.get("total_tokens")

        if input_tokens is not None:
            attributes["gen_ai.usage.input_tokens"] = input_tokens
        if output_tokens is not None:
            attributes["gen_ai.usage.output_tokens"] = output_tokens
        if total_tokens is not None:
            attributes["gen_ai.usage.total_tokens"] = total_tokens

        # TTFT
        events = []
        if ttft_ms is not None:
            events.append({
                "name": "first_token",
                "timestamp": start_wall + (ttft_ms / 1000),
                "attributes": {"time_to_first_token_ms": round(ttft_ms, 2)},
            })

        # Error event
        if status == "ERROR" and error_type:
            events.append({
                "name": "exception",
                "timestamp": start_wall + (elapsed_ms / 1000),
                "attributes": {
                    "exception.type": error_type,
                    "exception.message": error_message or "",
                },
            })

        # Build telemetry record
        record = {
            "trace_id": trace_ctx.trace_id,
            "span_id": trace_ctx.span_id,
            "parent_span_id": trace_ctx.parent_span_id,
            "trace_inherited": trace_ctx.inherited,
            "span_name": "llm.completion",
            "span_kind": "LLM",
            "start_time": start_wall,
            "end_time": start_wall + (elapsed_ms / 1000),
            "duration_ms": round(elapsed_ms, 2),
            "status": status,
            "http_status": http_status,
            "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
            "session_id": metadata.get("session_id"),
            "user_id": metadata.get("user_id"),
            "app_name": metadata.get("app_name", "unknown"),
            "business_scene": metadata.get("business_scene"),
            "attributes": attributes,
            "events": events,
            "error_type": error_type,
            "error_message": error_message,
            "payload": {
                "request": processed_payload,
                "response": response_payload,
            } if processed_payload or response_payload else None,
            "request_metadata": request_meta,
        }

        self.reporter.report(record)

    def _forward_headers(self, request: web.Request, preserve_traceparent: bool = True) -> dict:
        """Build headers to forward to upstream, stripping sensitive ones."""
        headers = {}
        for k, v in request.headers.items():
            kl = k.lower()
            if kl in self.config.sensitive_headers:
                # Keep Authorization for upstream but don't log it
                if kl == "authorization":
                    headers[k] = v
                continue
            if kl == "host":
                continue  # Let aiohttp set the correct host
            if kl == "traceparent" and not preserve_traceparent:
                continue
            headers[k] = v
        return headers

    def _filter_response_headers(self, upstream_headers) -> dict:
        """Filter response headers for the client response."""
        filtered = {}
        for k, v in upstream_headers.items():
            kl = k.lower()
            if kl in ("transfer-encoding", "content-encoding", "content-length"):
                continue  # aiohttp handles these
            filtered[k] = v
        return filtered

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        stats = self.reporter.get_stats()
        return web.json_response({
            "status": "healthy",
            "reporter_stats": stats,
            "upstream": self.config.upstream_url,
        })
