import { useState } from 'react'
import clsx from 'clsx'

interface PayloadViewerProps {
  payload: Record<string, unknown>
}

export default function PayloadViewer({ payload }: PayloadViewerProps) {
  const [view, setView] = useState<'request' | 'response'>('request')
  const [expanded, setExpanded] = useState(true)

  const req = (payload as Record<string, unknown>)?.request
  const resp = (payload as Record<string, unknown>)?.response
  const hasReq = req !== undefined && req !== null
  const hasResp = resp !== undefined && resp !== null

  const current = view === 'request' ? req : resp

  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <div className="flex gap-1">
          <button
            onClick={() => setView('request')}
            disabled={!hasReq}
            className={cn(
              'px-2 py-0.5 rounded text-xs',
              view === 'request'
                ? 'bg-primary-100 text-primary-700 font-medium'
                : 'text-gray-500',
              !hasReq && 'opacity-30',
            )}
          >
            Request
          </button>
          <button
            onClick={() => setView('response')}
            disabled={!hasResp}
            className={cn(
              'px-2 py-0.5 rounded text-xs',
              view === 'response'
                ? 'bg-primary-100 text-primary-700 font-medium'
                : 'text-gray-500',
              !hasResp && 'opacity-30',
            )}
          >
            Response
          </button>
        </div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-xs text-gray-400 hover:text-gray-600"
        >
          {expanded ? '− Collapse' : '+ Expand'}
        </button>
      </div>
      {expanded && current !== undefined && current !== null ? (
        <pre className="bg-gray-50 rounded p-2 text-xs overflow-x-auto max-h-64 font-mono">
          {JSON.stringify(current, null, 2)}
        </pre>
      ) : (
        <p className="text-xs text-gray-400 italic">
          {hasReq || hasResp
            ? 'Payload available — click expand to view'
            : 'No payload data (payload_strategy may be metadata_only)'}
        </p>
      )}
    </div>
  )
}

function cn(...args: (string | false | undefined)[]): string {
  return args.filter(Boolean).join(' ')
}
