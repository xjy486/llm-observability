import { useState, useEffect } from 'react'
import { fetchTraces } from '../api'
import type { TraceSummary } from '../types'
import TraceTable from '../components/TraceTable'
import clsx from 'clsx'

export default function TraceList() {
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [duration, setDuration] = useState(60)
  const [status, setStatus] = useState<string | undefined>(undefined)
  const [model, setModel] = useState<string | undefined>(undefined)
  const [messageId, setMessageId] = useState<string | undefined>(undefined)
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const limit = 50

  useEffect(() => {
    async function load() {
      setLoading(true)
      try {
        const data = await fetchTraces({
          durationMinutes: duration,
          status,
          model,
          message_id: messageId,
          limit,
          offset,
          sort_by: 'start_time',
          sort_order: 'desc',
        })
        setTraces(data.traces)
        setTotal(data.total)
      } catch (e) {
        console.error('Failed to load traces:', e)
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [duration, status, model, messageId, offset])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Traces</h1>
        <button
          onClick={() => setOffset(0)}
          className="btn btn-outline text-xs"
        >
          Refresh
        </button>
      </div>

      {/* Filters */}
      <div className="card flex flex-wrap items-center gap-3">
        <div className="flex gap-2">
          {[15, 60, 360, 1440].map((d) => (
            <button
              key={d}
              onClick={() => {
                setDuration(d)
                setOffset(0)
              }}
              className={cn('btn', duration === d ? 'btn-primary' : 'btn-outline')}
            >
              {d < 60 ? `${d}m` : d < 1440 ? `${d / 60}h` : '24h'}
            </button>
          ))}
        </div>

        <div className="flex gap-2">
          {['OK', 'ERROR'].map((s) => (
            <button
              key={s}
              onClick={() => {
                setStatus(status === s ? undefined : s)
                setOffset(0)
              }}
              className={cn('btn', status === s ? 'btn-primary' : 'btn-outline')}
            >
              {s}
            </button>
          ))}
        </div>

        <input
          type="text"
          placeholder="Filter by model..."
          value={model || ''}
          onChange={(e) => {
            setModel(e.target.value || undefined)
            setOffset(0)
          }}
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm flex-1 min-w-[200px]"
        />

        <input
          type="text"
          placeholder="Filter by message_id..."
          value={messageId || ''}
          onChange={(e) => {
            setMessageId(e.target.value || undefined)
            setOffset(0)
          }}
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm flex-1 min-w-[200px]"
        />
      </div>

      {/* Table */}
      <TraceTable traces={traces} loading={loading} />

      {/* Pagination */}
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-500">
          Showing {offset + 1}–{offset + traces.length} of {total}
        </span>
        <div className="flex gap-2">
          <button
            onClick={() => setOffset(Math.max(0, offset - limit))}
            disabled={offset === 0}
            className="btn btn-outline disabled:opacity-50"
          >
            ← Prev
          </button>
          <button
            onClick={() => setOffset(offset + limit)}
            disabled={offset + limit >= total}
            className="btn btn-outline disabled:opacity-50"
          >
            Next →
          </button>
        </div>
      </div>
    </div>
  )
}

function cn(...args: (string | false | undefined)[]): string {
  return args.filter(Boolean).join(' ')
}
