"""
Main proxy handler — transparent LLM API proxy with telemetry capture.

Handles:
- Non-streaming /v1/chat/completions
- Streaming SSE /v1/chat/completions
- W3C Trace Context propagation (P0-02: inject traceparent for downstream)
- TTFT/TTFC measurement (P0-04: TTFT = first token, TTFC = complete)
- Streaming response aggregation (P0-03: aggregate into standardized response)
- Streaming memory optimization (P1-05: incremental accumulator, no raw chunk storage)
- Token / usage extraction
- Error capture
- Payload capture with masking strategies
- Configurable gateway name (P1-04: not hardcoded)
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


class StreamingAccumulator:
    """Incremental accumulator for SSE streaming responses.

    P0-03: Aggregates streaming chunks into a standardized non-streaming response.
    P1-05: Does NOT store raw chunks — extracts only necessary fields incrementally.
    """

    def __init__(self):
        self.model: Optional[str] = None
        self.id: Optional[str] = None
        self.content_parts: list[str] = []  # concatenated content deltas
        self.reasoning_parts: list[str] = []  # reasoning/thinking content
        self.tool_calls: list[dict] = []
        self.finish_reason: Optional[str] = None
        self.usage: dict = {}
        self.chunk_count: int = 0
        self.first_chunk_received: bool = False

    def feed(self, chunk: dict) -> None:
        """Process a single SSE chunk incrementally."""
        self.chunk_count += 1

        if not self.model:
            self.model = chunk.get("model")
        if not self.id:
            self.id = chunk.get("id")

        choices = chunk.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            if isinstance(delta, dict):
                # Content delta
                content = delta.get("content")
                if content:
                    self.content_parts.append(content)

                # Reasoning content (some models)
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    self.reasoning_parts.append(reasoning)

                # Tool calls
                tc = delta.get("tool_calls")
                if tc:
                    for t in tc:
                        idx = t.get("index", len(self.tool_calls))
                        while len(self.tool_calls) <= idx:
                            self.tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if t.get("id"):
                            self.tool_calls[idx]["id"] = t["id"]
                        func = t.get("function", {})
                        if func.get("name"):
                            self.tool_calls[idx]["function"]["name"] += func["name"]
                        if func.get("arguments"):
                            self.tool_calls[idx]["function"]["arguments"] += func["arguments"]

            # Finish reason (usually on last chunk)
            fr = choice.get("finish_reason")
            if fr:
                self.finish_reason = fr

        # Usage (usually on last chunk)
        u = chunk.get("usage")
        if u and isinstance(u, dict):
            self.usage = u

    def build_response(self) -> dict:
        """Build a standardized non-streaming response object."""
        full_content = "".join(self.content_parts)
        message = {"role": "assistant", "content": full_content if full_content else None}
        if self.reasoning_parts:
            message["reasoning_content"] = "".join(self.reasoning_parts)
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls

        return {
            "id": self.id or "",
            "object": "chat.completion",
            "model": self.model or "unknown",
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self.finish_reason or "stop",
            }],
            "usage": self.usage,
            "stream_chunk_count": self.chunk_count,
        }


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
        headers = self._build_forward_headers(request)
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

        # P0-02: Build forward headers and INJECT traceparent for downstream
        forward_headers = self._build_forward_headers(request)
        forward_headers["traceparent"] = trace_ctx.to_traceparent()

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
                ttfc_ms=None,
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
        """Handle SSE streaming response with TTFT/TTFC measurement.

        P0-03: Uses StreamingAccumulator to build standardized response.
        P1-05: Does NOT store raw SSE chunks — only incremental accumulation.
        P0-04: Measures both TTFT (first token) and TTFC (complete).
        """
        # Create streaming response
        response = web.StreamResponse(
            status=upstream_resp.status,
            headers=self._filter_response_headers(upstream_resp.headers),
        )
        await response.prepare(request)

        ttft_ms = None
        accumulator = StreamingAccumulator()
        first_chunk_time = None

        try:
            async for line in upstream_resp.content:
                # Forward to client immediately
                await response.write(line)

                # Parse SSE data for telemetry
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                        accumulator.feed(chunk)
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
                response_payload=accumulator.build_response() if should_sample else None,
                ttft_ms=ttft_ms, ttfc_ms=elapsed_ms,
            )
            if not response.prepared:
                return web.Response(status=502, text="Streaming error")
            return response

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        is_error = upstream_resp.status >= 400

        # P0-03: Build standardized response from accumulator
        aggregated_response = accumulator.build_response()

        # P0-04: TTFC = total time from request to last byte
        ttfc_ms = elapsed_ms

        # Extract response metadata from aggregated response
        response_meta = extract_response_metadata(
            upstream_resp.status, aggregated_response, []
        )

        # Process response payload
        response_payload = None
        if should_sample and self.config.payload_strategy != "off":
            if self.config.payload_strategy == "metadata_only":
                response_payload = response_meta
            else:
                response_payload = aggregated_response
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
            response_payload=response_payload,
            ttft_ms=ttft_ms, ttfc_ms=ttfc_ms,
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
        """Handle non-streaming response.

        P0-04: For non-streaming, TTFT = TTFC = total response time.
        """
        resp_body_raw = await upstream_resp.read()

        try:
            resp_body = json.loads(resp_body_raw) if resp_body_raw else {}
        except json.JSONDecodeError:
            resp_body = {}

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        is_error = upstream_resp.status >= 400

        # P0-04: For non-streaming, TTFT = TTFC = total time
        ttft_ms = elapsed_ms if not is_error else None
        ttfc_ms = elapsed_ms

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
            response_payload=response_payload,
            ttft_ms=ttft_ms, ttfc_ms=ttfc_ms,
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
        ttfc_ms: Optional[float],
    ):
        """Build and queue telemetry record.

        P1-04: Uses self.config.gateway_name instead of hardcoded 'one-api-proxy'.
        P0-04: Reports both ttft_ms and ttfc_ms.
        """
        # Build attributes (OpenTelemetry GenAI semantic conventions)
        attributes = {
            "gen_ai.request.model": request_meta.get("model", "unknown"),
            "gen_ai.operation.name": "chat",
            "llm.stream": is_stream,
            "llm.gateway.name": self.config.gateway_name,
            "http.status_code": http_status,
        }

        # Add response model
        if isinstance(response_payload, dict):
            resp_model = response_payload.get("model")
            if resp_model:
                attributes["gen_ai.response.model"] = resp_model

        # Tokens
        resp_meta = response_payload if isinstance(response_payload, dict) else {}
        input_tokens = None
        output_tokens = None
        total_tokens = None

        if resp_meta:
            usage = resp_meta.get("usage", {})
            if isinstance(usage, dict) and usage:
                input_tokens = usage.get("prompt_tokens")
                output_tokens = usage.get("completion_tokens")
                total_tokens = usage.get("total_tokens")
            # Also check top-level fields
            if input_tokens is None:
                input_tokens = resp_meta.get("input_tokens")
            if output_tokens is None:
                output_tokens = resp_meta.get("output_tokens")
            if total_tokens is None:
                total_tokens = resp_meta.get("total_tokens")

        if input_tokens is not None:
            attributes["gen_ai.usage.input_tokens"] = input_tokens
        if output_tokens is not None:
            attributes["gen_ai.usage.output_tokens"] = output_tokens
        if total_tokens is not None:
            attributes["gen_ai.usage.total_tokens"] = total_tokens

        # TTFT event
        events = []
        if ttft_ms is not None:
            events.append({
                "name": "first_token",
                "timestamp": start_wall + (ttft_ms / 1000),
                "attributes": {"time_to_first_token_ms": round(ttft_ms, 2)},
            })

        # TTFC event
        if ttfc_ms is not None:
            events.append({
                "name": "response_complete",
                "timestamp": start_wall + (ttfc_ms / 1000),
                "attributes": {"time_to_complete_ms": round(ttfc_ms, 2)},
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
            "ttfc_ms": round(ttfc_ms, 2) if ttfc_ms is not None else None,
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

    def _build_forward_headers(self, request: web.Request) -> dict:
        """Build headers to forward to upstream.

        P0-02: Strips incoming traceparent; caller must inject new one.
        Sensitive headers (except Authorization) are stripped.
        """
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
            if kl == "traceparent":
                continue  # P0-02: stripped here, injected by caller with new span context
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
            "gateway_name": self.config.gateway_name,
        })
