interface DataPoint {
  x: number
  y: number
}

interface MiniChartProps {
  data: DataPoint[]
  color: string
  label?: string
}

export default function MiniChart({ data, color, label }: MiniChartProps) {
  if (data.length === 0) {
    return (
      <div className="h-32 flex items-center justify-center text-gray-400 text-sm">
        No data
      </div>
    )
  }

  const width = 500
  const height = 120
  const padding = { top: 10, right: 10, bottom: 20, left: 40 }
  const chartW = width - padding.left - padding.right
  const chartH = height - padding.top - padding.bottom

  const xs = data.map((d) => d.x)
  const ys = data.map((d) => d.y)
  const xMin = Math.min(...xs)
  const xMax = Math.max(...xs)
  const yMax = Math.max(...ys, 1)

  const scaleX = (x: number) =>
    padding.left + ((x - xMin) / (xMax - xMin || 1)) * chartW
  const scaleY = (y: number) =>
    padding.top + chartH - (y / yMax) * chartH

  const pathData = data
    .map((d, i) => `${i === 0 ? 'M' : 'L'} ${scaleX(d.x)},${scaleY(d.y)}`)
    .join(' ')

  const areaPath =
    `${pathData} L ${scaleX(data[data.length - 1].x)},${scaleY(0)} L ${scaleX(data[0].x)},${scaleY(0)} Z`

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-32">
        <defs>
          <linearGradient id={`grad-${color.replace('#', '')}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.3" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {/* Grid lines */}
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <line
            key={t}
            x1={padding.left}
            y1={padding.top + chartH * t}
            x2={width - padding.right}
            y2={padding.top + chartH * t}
            stroke="#e5e7eb"
            strokeWidth="1"
          />
        ))}
        {/* Y-axis labels */}
        {yMax > 0 && (
          <text x={padding.left - 5} y={scaleY(yMax) + 3} textAnchor="end" className="text-[10px] fill-gray-400">
            {Math.round(yMax)}
          </text>
        )}
        <text x={padding.left - 5} y={scaleY(0) + 3} textAnchor="end" className="text-[10px] fill-gray-400">
          0
        </text>
        {/* Area */}
        <path d={areaPath} fill={`url(#grad-${color.replace('#', '')})`} />
        {/* Line */}
        <path d={pathData} fill="none" stroke={color} strokeWidth="2" />
      </svg>
      {label && (
        <div className="text-xs text-gray-500 mt-1">{label}</div>
      )}
    </div>
  )
}
