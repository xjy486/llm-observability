import { useState, useEffect } from 'react'
import { fetchMetrics, fetchTimeSeries, fetchModels } from '../api'
import type { MetricsSummary, TimeSeriesPoint, ModelInfo } from '../types'
import StatCard from '../components/StatCard'
import MiniChart from '../components/MiniChart'

export default function Dashboard() {
  const [metrics, setMetrics] = useState<MetricsSummary | null>(null)
  const [timeseries, setTimeseries] = useState<TimeSeriesPoint[]>([])
  const [models, setModels] = useState<ModelInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [duration, setDuration] = useState(60)

  useEffect(() => {
    async function load() {
      setLoading(true)
      try {
        const [m, ts, md] = await Promise.all([
          fetchMetrics(duration),
          fetchTimeSeries(duration, Math.max(60, duration)),
          fetchModels(),
        ])
        setMetrics(m)
        setTimeseries(ts)
        setModels(md)
      } catch (e) {
        console.error('Failed to load dashboard data:', e)
      } finally {
        setLoading(false)
      }
    }
    load()
    const interval = setInterval(load, 30000)
    return () => clearInterval(interval)
  }, [duration])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <div className="flex gap-2">
          {[15, 60, 360, 1440].map((d) => (
            <button
              key={d}
              onClick={() => setDuration(d)}
              className={cn('btn', duration === d ? 'btn-primary' : 'btn-outline')}
            >
              {d < 60 ? `${d}m` : d < 1440 ? `${d / 60}h` : '24h'}
            </button>
          ))}
        </div>
      </div>

      {loading && !metrics ? (
        <div className="text-center py-12 text-gray-400">Loading...</div>
      ) : metrics ? (
        <>
          {/* Stat cards */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            <StatCard label="Total Requests" value={metrics.total_requests} />
            <StatCard
              label="Error Rate"
              value={`${metrics.error_rate}%`}
              error={metrics.error_rate > 5}
            />
            <StatCard label="p50 Latency" value={`${metrics.p50_latency_ms}ms`} />
            <StatCard label="p95 Latency" value={`${metrics.p95_latency_ms}ms`} />
            <StatCard
              label="Avg TTFT"
              value={metrics.avg_ttft_ms ? `${metrics.avg_ttft_ms}ms` : '—'}
            />
            <StatCard
              label="Total Tokens"
              value={metrics.total_tokens.toLocaleString()}
            />
          </div>

          {/* Charts row */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="card">
              <h3 className="text-sm font-semibold text-gray-700 mb-3">
                Request Volume (per {Math.max(60, duration)}s)
              </h3>
              <MiniChart
                data={timeseries.map((p) => ({ x: p.bucket, y: p.count }))}
                color="#3b82f6"
                label="requests"
              />
            </div>
            <div className="card">
              <h3 className="text-sm font-semibold text-gray-700 mb-3">
                Error Rate Over Time
              </h3>
              <MiniChart
                data={timeseries.map((p) => ({
                  x: p.bucket,
                  y: p.count > 0 ? (p.errors / p.count) * 100 : 0,
                }))}
                color="#ef4444"
                label="%"
              />
            </div>
            <div className="card">
              <h3 className="text-sm font-semibold text-gray-700 mb-3">
                Avg Latency Over Time
              </h3>
              <MiniChart
                data={timeseries.map((p) => ({ x: p.bucket, y: p.avg_latency || 0 }))}
                color="#f59e0b"
                label="ms"
              />
            </div>
            <div className="card">
              <h3 className="text-sm font-semibold text-gray-700 mb-3">
                Token Usage Over Time
              </h3>
              <MiniChart
                data={timeseries.map((p) => ({ x: p.bucket, y: p.tokens || 0 }))}
                color="#8b5cf6"
                label="tokens"
              />
            </div>
          </div>

          {/* Model breakdown */}
          <div className="card">
            <h3 className="text-sm font-semibold text-gray-700 mb-3">Model Breakdown</h3>
            {models.length === 0 ? (
              <p className="text-gray-400 text-sm">No model data yet</p>
            ) : (
              <table className="w-full text-sm">
                <thead className="text-gray-500 border-b border-gray-200">
                  <tr>
                    <th className="text-left py-2">Model</th>
                    <th className="text-right py-2">Requests</th>
                    <th className="text-right py-2">Errors</th>
                    <th className="text-right py-2">Error Rate</th>
                  </tr>
                </thead>
                <tbody>
                  {models.map((m) => (
                    <tr key={m.model} className="border-b border-gray-100">
                      <td className="py-2 font-mono text-xs">{m.model}</td>
                      <td className="py-2 text-right">{m.count}</td>
                      <td className="py-2 text-right">
                        {m.errors > 0 ? (
                          <span className="text-red-600">{m.errors}</span>
                        ) : (
                          '0'
                        )}
                      </td>
                      <td className="py-2 text-right">
                        {m.count > 0 ? ((m.errors / m.count) * 100).toFixed(1) : '0.0'}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      ) : null}
    </div>
  )
}

// simple classnames helper
function cn(...args: (string | false | undefined)[]): string {
  return args.filter(Boolean).join(' ')
}
