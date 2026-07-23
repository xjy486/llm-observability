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
  ttft_ms: number | null
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
  input_tokens: number
  output_tokens: number
  total_tokens: number
  spans: SpanRecord[]
}

export interface MetricsSummary {
  total_requests: number
  error_count: number
  error_rate: number
  p50_latency_ms: number
  p95_latency_ms: number
  p99_latency_ms: number
  avg_ttft_ms: number | null
  p50_ttft_ms: number | null
  p95_ttft_ms: number | null
  total_input_tokens: number
  total_output_tokens: number
  total_tokens: number
  unique_models: number
  unique_users: number
  unique_sessions: number
}

export interface TimeSeriesPoint {
  bucket: number
  count: number
  errors: number
  avg_latency: number
  tokens: number
  avg_ttft: number | null
}

export interface ModelInfo {
  model: string
  count: number
  errors: number
}

export interface TraceListResponse {
  traces: TraceSummary[]
  total: number
  limit: number
  offset: number
}
