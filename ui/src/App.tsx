import { Outlet, Link, useLocation } from 'react-router-dom'
import { Activity, LayoutDashboard, List } from 'lucide-react'
import clsx from 'clsx'

const navItems = [
  { path: '/', label: 'Dashboard', icon: LayoutDashboard },
  { path: '/traces', label: 'Traces', icon: List },
]

export default function App() {
  const location = useLocation()

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className="w-60 bg-gray-900 text-gray-100 flex flex-col fixed h-full">
        <div className="px-5 py-5 flex items-center gap-2">
          <Activity className="w-6 h-6 text-primary-400" />
          <span className="font-bold text-lg">LLM Observability</span>
        </div>
        <nav className="flex-1 px-3 py-2">
          {navItems.map((item) => {
            const active = location.pathname === item.path
            const Icon = item.icon
            return (
              <Link
                key={item.path}
                to={item.path}
                className={clsx(
                  'flex items-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors mb-1',
                  active
                    ? 'bg-primary-600 text-white'
                    : 'text-gray-400 hover:bg-gray-800 hover:text-white',
                )}
              >
                <Icon className="w-4 h-4" />
                {item.label}
              </Link>
            )
          })}
        </nav>
        <div className="px-5 py-3 text-xs text-gray-500">
          v0.1.0 · MVP
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 ml-60 p-6">
        <Outlet />
      </main>
    </div>
  )
}
