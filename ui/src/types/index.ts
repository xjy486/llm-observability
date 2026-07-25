export interface SpanRecord {
  trace_id: string
  span_id: string
  parent_span_id: string | null
  span_name: string
  span_kind: string
  start_time: number
  end_time: number
  duration_ms: number
  status: string
  http_status: number | null
  ttft_ms: number | null          // Time To First Token (streaming only, NULL for non-streaming)
  first_chunk_ms: number | null   // Time To First SSE Chunk (streaming only, NULL for non-streaming)
  session_id: string | null
  user_id: string | null
  app_name: string | null
  business_scene: string | null
  attributes: Record<string, unknown>
  events: Event[]
  error_type: string | null
  error_message: string | null
  payload: Record<string, unknown> | null
  request_metadata: Record<string, unknown> | null
  payload_ref: string | null
  trace_inherited: boolean
  model: string | null
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  is_stream: boolean | null
}

export interface Event {
  name: string
  timestamp: number
  attributes: Record<string, unknown>
}

export interface TraceSummary {
  trace_id: string
  root_span_id: string
  start_time: number
  end_time: number
  duration_ms: number
  status: string
  session_id: string | null
  user_id: string | null
  app_name: string | null
  business_scene: string | null
  span_count: number
  llm_call_count: number
  tool_call_count: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  model: string | null
  input_summary: string | null
  output_summary: string | null
  error_type: string | null
}

export interface TraceDetail {
  trace_id: string
  root_span_id: string
  start_time: number
  end_time: number
  duration_ms: number
  status: string
  session_id: string | null
  user_id: string | null
  app_name: string | null
  business_scene: string | null
  span_count: number
  llm_call_count: number
  tool_call_count: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  spans: SpanRecord[]
}

export interface MetricsSummary {
  // Trace-level
  trace_count: number
  error_count: number
  error_rate: number
  // LLM Call-level
  llm_call_count: number
  p50_latency_ms: number
  p95_latency_ms: number
  p99_latency_ms: number
  avg_ttft_ms: number | null
  p50_ttft_ms: number | null
  p95_ttft_ms: number | null
  avg_first_chunk_ms: number | null
  p50_first_chunk_ms: number | null
  p95_first_chunk_ms: number | null
  // Tokens
  total_input_tokens: number
  total_output_tokens: number
  total_tokens: number
  // Span-level (debugging)
  span_count: number
  // Dimensional
  unique_models: number
  unique_users: number
  unique_sessions: number
  // Tool-level metrics (Phase 2.2)
  tool_call_count: number
  tool_error_count: number
  tool_error_rate: number
  p50_tool_latency_ms: number
  p95_tool_latency_ms: number
  p99_tool_latency_ms: number
}

export interface TimeSeriesPoint {
  bucket: number
  trace_count: number           // P1-NEW-01: distinct traces in bucket
  trace_error_count: number     // P1-NEW-01: traces with any ERROR span
  llm_call_count: number        // P1-NEW-01: LLM spans in bucket
  llm_error_count: number       // P1-NEW-01: ERROR LLM spans
  llm_avg_latency_ms: number    // P1-NEW-01: avg duration_ms of LLM spans
  avg_ttft_ms: number | null    // P1-NEW-01: avg ttft_ms of LLM spans (streaming only)
  avg_first_chunk_ms: number | null  // P1-NEW-01: avg first_chunk_ms (streaming only)
  span_count: number            // P1-NEW-01: total spans in bucket
  tokens: number
  // Tool-level metrics (Phase 2.2)
  tool_call_count: number
  tool_error_count: number
  tool_avg_latency_ms: number | null
}

export interface ModelInfo {
  model: string
  span_count: number
  llm_call_count: number
  trace_count: number
  llm_errors: number            // P0-NEW-01: renamed from "errors" for clarity
}

export interface TraceListResponse {
  traces: TraceSummary[]
  total: number
  limit: number
  offset: number
}
