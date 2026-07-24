import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { fetchTraceDetail } from '../api'
import type { TraceDetail, SpanRecord } from '../types'
import SpanTree from '../components/SpanTree'
import PayloadViewer from '../components/PayloadViewer'
import clsx from 'clsx'

export default function TraceDetail() {
  const { traceId } = useParams<{ traceId: string }>()
  const [trace, setTrace] = useState<TraceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedSpan, setSelectedSpan] = useState<SpanRecord | null>(null)

  useEffect(() => {
    async function load() {
      if (!traceId) return
      setLoading(true)
      setError(null)
      try {
        const data = await fetchTraceDetail(traceId)
        setTrace(data)
        if (data.spans.length > 0) {
          const root = data.spans.find((s) => !s.parent_span_id) || data.spans[0]
          setSelectedSpan(root)
        }
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e)
        setError(msg)
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [traceId])

  if (loading) {
    return <div className="text-center py-12 text-gray-400">Loading trace...</div>
  }

  if (error || !trace) {
    return (
      <div className="text-center py-12">
        <p className="text-red-500 mb-2">Error: {error || 'Trace not found'}</p>
        <Link to="/traces" className="text-primary-600 hover:underline">
          ← Back to traces
        </Link>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Link to="/traces" className="text-gray-400 hover:text-gray-600">
          ← Back
        </Link>
        <h1 className="text-2xl font-bold">Trace Detail</h1>
      </div>

      {/* Trace metadata */}
      <div className="card">
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
          <div>
            <div className="text-xs text-gray-500">Trace ID</div>
            <div className="font-mono text-xs truncate">{trace.trace_id}</div>
          </div>
          <div>
            <div className="text-xs text-gray-500">Status</div>
            <span
              className={clsx(
                'tag',
                trace.status === 'ERROR' ? 'tag-error' : 'tag-ok',
              )}
            >
              {trace.status}
            </span>
          </div>
          <div>
            <div className="text-xs text-gray-500">Duration</div>
            <div className="font-mono text-sm">
              {trace.duration_ms < 1000
                ? `${Math.round(trace.duration_ms)}ms`
                : `${(trace.duration_ms / 1000).toFixed(2)}s`}
            </div>
          </div>
          <div>
            <div className="text-xs text-gray-500">Spans</div>
            <div className="text-sm">{trace.span_count}</div>
          </div>
          <div>
            <div className="text-xs text-gray-500">LLM Calls</div>
            <div className="text-sm">{trace.llm_call_count}</div>
          </div>
          <div>
            <div className="text-xs text-gray-500">Total Tokens</div>
            <div className="text-sm">{trace.total_tokens.toLocaleString()}</div>
          </div>
        </div>
        {(trace.session_id || trace.user_id || trace.app_name) && (
          <div className="mt-3 pt-3 border-t border-gray-100 flex flex-wrap gap-4">
            {trace.session_id && (
              <div>
                <span className="text-xs text-gray-500">Session:</span>
                <span className="ml-1 text-xs font-mono">{trace.session_id}</span>
              </div>
            )}
            {trace.user_id && (
              <div>
                <span className="text-xs text-gray-500">User:</span>
                <span className="ml-1 text-xs font-mono">{trace.user_id}</span>
              </div>
            )}
            {trace.app_name && (
              <div>
                <span className="text-xs text-gray-500">App:</span>
                <span className="ml-1 text-xs">{trace.app_name}</span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Span tree + detail */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2">
          <div className="card">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-gray-700">Span Tree</h3>
              <span className="text-xs text-gray-400">Click a span to inspect</span>
            </div>
            <SpanTree
              spans={trace.spans}
              selectedSpanId={selectedSpan?.span_id}
              onSelect={setSelectedSpan}
            />
          </div>
        </div>

        {/* Span detail panel */}
        <div className="lg:col-span-1">
          {selectedSpan ? (
            <SpanDetailPanel span={selectedSpan} />
          ) : (
            <div className="card text-center text-gray-400">
              Select a span to view details
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function SpanDetailPanel({ span }: { span: SpanRecord }) {
  return (
    <div className="card space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-gray-700">Span Details</h3>
        <div className="font-mono text-xs text-primary-600 mt-1">{span.span_id}</div>
      </div>

      <div className="space-y-2 text-xs">
        <div className="grid grid-cols-2 gap-2">
          <div>
            <span className="text-gray-500">Kind:</span>
            <span className="ml-1 font-medium">{span.span_kind}</span>
          </div>
          <div>
            <span className="text-gray-500">Name:</span>
            <span className="ml-1 font-mono">{span.span_name}</span>
          </div>
          <div>
            <span className="text-gray-500">Duration:</span>
            <span className="ml-1 font-mono">
              {span.duration_ms < 1000
                ? `${Math.round(span.duration_ms)}ms`
                : `${(span.duration_ms / 1000).toFixed(2)}s`}
            </span>
          </div>
          <div>
            <span className="text-gray-500">HTTP:</span>
            <span className="ml-1 font-mono">{span.http_status || '—'}</span>
          </div>
          {span.ttft_ms !== null && (
            <div>
              <span className="text-gray-500">TTFT:</span>
              <span className="ml-1 font-mono">{Math.round(span.ttft_ms)}ms</span>
            </div>
          )}
          {span.model && (
            <div>
              <span className="text-gray-500">Model:</span>
              <span className="ml-1 font-mono">{span.model}</span>
            </div>
          )}
          {(span.input_tokens !== null || span.output_tokens !== null) && (
            <div>
              <span className="text-gray-500">Tokens:</span>
              <span className="ml-1 font-mono">
                {span.input_tokens || 0} in / {span.output_tokens || 0} out
              </span>
            </div>
          )}
        </div>

        {/* GATEWAY-specific fields */}
        {span.span_kind === 'GATEWAY' && (
          <div className="grid grid-cols-2 gap-2 pt-2 border-t border-gray-100">
            <div>
              <span className="text-gray-500">Gateway:</span>
              <span className="ml-1 font-mono">
                {span.attributes?.['llm.gateway.name'] || '—'}
              </span>
            </div>
            {span.first_chunk_ms !== null && (
              <div>
                <span className="text-gray-500">First Chunk:</span>
                <span className="ml-1 font-mono">{Math.round(span.first_chunk_ms)}ms</span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Payload viewer */}
      {span.payload && (
        <PayloadViewer payload={span.payload} />
      )}

      {/* Events */}
      {span.events.length > 0 && (
        <div>
          <div className="text-xs text-gray-500 mb-1">Events</div>
          <div className="space-y-1">
            {span.events.map((ev, i) => (
              <div key={i} className="text-xs bg-gray-50 rounded p-2">
                <span className="font-medium">{ev.name}</span>
                <span className="text-gray-400 ml-2">
                  {new Date(ev.timestamp * 1000).toLocaleTimeString()}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Error info */}
      {span.error_type && (
        <div className="bg-red-50 rounded p-2">
          <div className="text-xs font-medium text-red-700">{span.error_type}</div>
          {span.error_message && (
            <div className="text-xs text-red-600 mt-1 font-mono break-all">
              {span.error_message}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
