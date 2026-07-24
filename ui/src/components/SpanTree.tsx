import type { SpanRecord } from '../types'
import clsx from 'clsx'

interface SpanTreeProps {
  spans: SpanRecord[]
  selectedSpanId?: string
  onSelect: (span: SpanRecord) => void
}

interface TreeNode {
  span: SpanRecord
  children: TreeNode[]
  depth: number
}

function buildTree(spans: SpanRecord[]): TreeNode[] {
  const byId = new Map<string, TreeNode>()
  const roots: TreeNode[] = []

  for (const s of spans) {
    byId.set(s.span_id, { span: s, children: [], depth: 0 })
  }

  for (const s of spans) {
    const node = byId.get(s.span_id)!
    if (s.parent_span_id && byId.has(s.parent_span_id)) {
      const parent = byId.get(s.parent_span_id)!
      parent.children.push(node)
    } else {
      roots.push(node)
    }
  }

  function setDepth(nodes: TreeNode[], depth: number) {
    for (const n of nodes) {
      n.depth = depth
      setDepth(n.children, depth + 1)
    }
  }
  setDepth(roots, 0)

  return roots
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function kindColor(kind: string): string {
  switch (kind) {
    case 'LLM':
      return 'tag-llm'
    case 'AGENT':
      return 'tag-agent'
    case 'TOOL':
      return 'tag-tool'
    case 'GATEWAY':
      return 'tag-gateway'
    default:
      return 'bg-gray-100 text-gray-800'
  }
}

// Compute relative timing for waterfall
function WaterfallBar({
  span,
  traceStart,
  traceEnd,
}: {
  span: SpanRecord
  traceStart: number
  traceEnd: number
}) {
  const totalDuration = traceEnd - traceStart
  if (totalDuration <= 0) return null
  const startPct = ((span.start_time - traceStart) / totalDuration) * 100
  const widthPct = ((span.end_time - span.start_time) / totalDuration) * 100
  const color =
    span.status === 'ERROR'
      ? 'bg-red-400'
      : span.span_kind === 'LLM'
        ? 'bg-blue-400'
        : span.span_kind === 'AGENT'
          ? 'bg-purple-400'
          : span.span_kind === 'TOOL'
            ? 'bg-orange-400'
            : span.span_kind === 'GATEWAY'
              ? 'bg-teal-400'
              : 'bg-gray-400'

  return (
    <div className="relative h-5 bg-gray-100 rounded">
      <div
        className={clsx('absolute h-5 rounded', color)}
        style={{
          left: `${Math.max(0, startPct)}%`,
          width: `${Math.max(0.5, widthPct)}%`,
        }}
      />
    </div>
  )
}

export default function SpanTree({ spans, selectedSpanId, onSelect }: SpanTreeProps) {
  const tree = buildTree(spans)
  const traceStart = Math.min(...spans.map((s) => s.start_time))
  const traceEnd = Math.max(...spans.map((s) => s.end_time))

  function renderNodes(nodes: TreeNode[]): React.ReactNode {
    return nodes.map((node) => (
      <div key={node.span.span_id}>
        <div
          onClick={() => onSelect(node.span)}
          className={clsx(
            'grid grid-cols-12 gap-2 items-center py-1.5 px-2 rounded cursor-pointer transition-colors',
            'hover:bg-gray-50',
            selectedSpanId === node.span.span_id && 'bg-primary-50',
          )}
          style={{ paddingLeft: `${node.depth * 16 + 8}px` }}
        >
          {/* Indent + name */}
          <div className="col-span-4 flex items-center gap-1">
            {node.children.length > 0 && (
              <span className="text-gray-400 text-xs">▾</span>
            )}
            <span className={clsx('tag', kindColor(node.span.span_kind))}>
              {node.span.span_kind}
            </span>
            <span className="text-xs font-mono text-gray-700 truncate">
              {node.span.span_name}
            </span>
          </div>
          {/* Duration */}
          <div className="col-span-1 text-right text-xs font-mono text-gray-500">
            {formatDuration(node.span.duration_ms)}
          </div>
          {/* Waterfall */}
          <div className="col-span-6">
            <WaterfallBar
              span={node.span}
              traceStart={traceStart}
              traceEnd={traceEnd}
            />
          </div>
          {/* Status */}
          <div className="col-span-1">
            <span
              className={clsx(
                'tag',
                node.span.status === 'ERROR' ? 'tag-error' : 'tag-ok',
              )}
            >
              {node.span.status === 'ERROR' ? 'ERR' : 'OK'}
            </span>
          </div>
        </div>
        {node.children.length > 0 && renderNodes(node.children)}
      </div>
    ))
  }

  return <div className="space-y-0">{renderNodes(tree)}</div>
}