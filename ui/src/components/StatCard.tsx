interface StatCardProps {
  label: string
  value: string | number
  error?: boolean
}

export default function StatCard({ label, value, error }: StatCardProps) {
  return (
    <div className="stat-card">
      <div className="text-xs text-gray-500 font-medium uppercase tracking-wide">
        {label}
      </div>
      <div
        className={`text-2xl font-bold mt-1 ${
          error ? 'text-red-600' : 'text-gray-900'
        }`}
      >
        {value}
      </div>
    </div>
  )
}
