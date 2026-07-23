import axios from 'axios'
import type {
  TraceListResponse,
  TraceDetail,
  MetricsSummary,
  TimeSeriesPoint,
  ModelInfo,
} from '../types'

const client = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
})

export async function fetchMetrics(durationMinutes = 60): Promise<MetricsSummary> {
  const { data } = await client.get('/metrics', { params: { durationMinutes } })
  return data
}

export async function fetchTimeSeries(
  durationMinutes = 60,
  intervalSeconds = 60,
): Promise<TimeSeriesPoint[]> {
  const { data } = await client.get('/timeseries', {
    params: { durationMinutes, intervalSeconds },
  })
  return data
}

export async function fetchTraces(params: {
  durationMinutes?: number
  status?: string
  model?: string
  session_id?: string
  user_id?: string
  limit?: number
  offset?: number
  sort_by?: string
  sort_order?: string
}): Promise<TraceListResponse> {
  const { data } = await client.get('/traces', { params })
  return data
}

export async function fetchTraceDetail(traceId: string): Promise<TraceDetail> {
  const { data } = await client.get(`/traces/${traceId}`)
  return data
}

export async function fetchModels(): Promise<ModelInfo[]> {
  const { data } = await client.get('/models')
  return data.models
}

export async function fetchSessionTraces(sessionId: string): Promise<TraceListResponse> {
  const { data } = await client.get(`/sessions/${sessionId}/traces`)
  return data
}
