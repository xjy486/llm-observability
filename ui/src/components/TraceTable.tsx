import { Link } from 'react-router-dom'
import type { TraceSummary } from '../types'
import clsx from 'clsx'

interface TraceTableProps {
  traces: TraceSummary[]
  loading: boolean
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleString()
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

export default function TraceTable({ traces, loading }: TraceTableProps) {
  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading...</div>
  }

  if (traces.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400">
        No traces found. Send some LLM requests through the proxy to see them here.
      </div>
    )
  }

  return (
    <div className="card overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-gray-500 border-b border-gray-200">
          <tr>
            <th className="text-left py-2 px-2">Trace ID</th>
            <th className="text-left py-2 px-2">Time</th>
            <th className="text-left py-2 px-2">Status</th>
            <th className="text-right py-2 px-2">Duration</th>
            <th className="text-right py-2 px-2">Spans</th>
            <th className="text-right py-2 px-2">LLM Calls</th>
            <th className="text-right py-2 px-2">Tokens</th>
            <th className="text-left py-2 px-2">Model</th>
            <th className="text-left py-2 px-2">Input Summary</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((t) => (
            <tr
              key={t.trace_id}
              className="border-b border-gray-100 hover:bg-gray-50 cursor-pointer"
            >
              <td className="py-2 px-2">
                <Link
                  to={`/traces/${t.trace_id}`}
                  className="font-mono text-xs text-primary-600 hover:underline"
                >
                  {t.trace_id.substring(0, 16)}...
                </Link>
              </td>
              <td className="py-2 px-2 text-xs text-gray-500">
                {formatTime(t.start_time)}
              </td>
              <td className="py-2 px-2">
                <span
                  className={clsx(
                    'tag',
                    t.status === 'ERROR' ? 'tag-error' : 'tag-ok',
                  )}
                >
                  {t.status}
                </span>
              </td>
              <td className="py-2 px-2 text-right font-mono text-xs">
                {formatDuration(t.duration_ms)}
              </td>
              <td className="py-2 px-2 text-right">{t.span_count}</td>
              <td className="py-2 px-2 text-right">{t.llm_call_count}</td>
              <td className="py-2 px-2 text-right">
                {t.total_tokens > 0 ? t.total_tokens.toLocaleString() : '—'}
              </td>
              <td className="py-2 px-2 font-mono text-xs text-gray-600">
                {t.model || '—'}
              </td>
              <td className="py-2 px-2 text-xs text-gray-500 max-w-xs truncate">
                {t.input_summary || '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
