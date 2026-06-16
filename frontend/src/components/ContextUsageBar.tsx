import { useEffect, useRef, useState } from 'react'
import type { ContextUsage } from '../lib/api/types'

interface Props {
  usage: ContextUsage | null
}

const COLOR_STOPS = {
  safe: '#22c55e',
  warm: '#eab308',
  near: '#f97316',
  danger: '#ef4444',
} as const

function clampPercentage(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(100, value))
}

function formatTokens(value: number): string {
  if (value >= 1000) {
    const compact = value >= 10000 ? Math.round(value / 1000) : Math.round((value / 1000) * 10) / 10
    return `${compact}k`
  }
  return String(value)
}

function getBarColor(percentage: number): string {
  if (percentage >= 90) return COLOR_STOPS.danger
  if (percentage >= 80) return COLOR_STOPS.near
  if (percentage >= 60) return COLOR_STOPS.warm
  return COLOR_STOPS.safe
}

function getStatusLabel(percentage: number): string {
  if (percentage >= 90) return 'Compaction imminent'
  if (percentage >= 80) return 'Approaching limit'
  if (percentage >= 60) return 'Getting warm'
  return 'Plenty of room'
}

export function ContextUsageBar({ usage }: Props) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  if (!usage) return null

  const percentage = clampPercentage(usage.percentage)
  const color = getBarColor(percentage)
  const pulseClass = percentage >= 90 ? 'animate-pulse' : ''

  const messages = usage.breakdown.find((item) => item.key === 'messages')?.tokens ?? 0
  const system = usage.breakdown
    .filter((item) => ['runtime_prompt', 'skills', 'memory_retrieval'].includes(item.key))
    .reduce((total, item) => total + item.tokens, 0)
  const tools = usage.breakdown
    .filter((item) => ['builtin_tools', 'mcp_tools', 'deferred_tools'].includes(item.key))
    .reduce((total, item) => total + item.tokens, 0)
  const summaryRows = [
    { label: 'Messages', value: messages },
    { label: 'System', value: system },
    { label: 'Tools', value: tools, toolCalls: usage.tool_call_count ?? 0 },
    { label: 'Free', value: Math.max(0, usage.free_tokens) },
  ]

  return (
    <div ref={containerRef} className="relative flex items-center">
      {/* Mini ring button — h-7 matches the permission dropdown height */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.03] transition-colors hover:border-white/[0.12] focus:outline-none ${pulseClass}`}
        aria-label={`Context usage ${Math.round(percentage)}% — click to ${open ? 'hide' : 'show'} breakdown`}
        aria-expanded={open}
      >
        <span
          className="inline-flex h-[14px] w-[14px] items-center justify-center rounded-full"
          style={{
            background: `conic-gradient(${color} ${percentage * 3.6}deg, rgba(130, 147, 112, 0.18) 0deg)`,
          }}
        >
          <span className="h-[8px] w-[8px] rounded-full bg-[#11100f]" />
        </span>
      </button>

      {/* Upward popover */}
      {open && (
        <div className="absolute bottom-full right-0 z-40 mb-2.5 w-56 overflow-hidden rounded-xl border border-white/[0.1] bg-[#171615] shadow-2xl shadow-black/50">
          {/* Header strip */}
          <div className="flex items-center justify-between border-b border-white/[0.06] px-3 py-2">
            <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-neutral-500">
              Context
            </span>
            <span className="text-[10px] font-semibold tabular-nums" style={{ color }}>
              {Math.round(percentage)}% · {formatTokens(usage.used)} / {formatTokens(usage.limit)}
            </span>
          </div>

          {/* Progress bar */}
          <div className="px-3 pt-2.5">
            <div className="h-1 w-full overflow-hidden rounded-full bg-white/[0.06]">
              <div
                className="h-full rounded-full transition-all duration-300"
                style={{ width: `${percentage}%`, background: color }}
              />
            </div>
            <div className="mt-1 flex items-center justify-between">
              <span className="text-[9px] text-neutral-600">{getStatusLabel(percentage)}</span>
              <span className="text-[9px] text-neutral-700">cap {formatTokens(usage.compaction_at)}</span>
            </div>
          </div>

          {/* Token breakdown grid */}
          <div className="grid grid-cols-4 gap-x-2 px-3 pb-3 pt-2.5">
            {summaryRows.map((row) => (
              <div key={row.label}>
                <p className="text-[8px] font-semibold uppercase tracking-[0.12em] text-neutral-700">
                  {row.label}
                </p>
                <p className="mt-0.5 text-[11px] font-medium tabular-nums text-neutral-300">
                  {formatTokens(row.value)}
                </p>
                {'toolCalls' in row && (row.toolCalls ?? 0) > 0 && (
                  <p className="mt-0.5 text-[9px] font-medium tabular-nums text-neutral-600">
                    {row.toolCalls} calls
                  </p>
                )}
              </div>
            ))}
          </div>

          {usage.estimator && (
            <p className="border-t border-white/[0.05] px-3 py-1.5 text-[9px] text-neutral-700" title={usage.notes}>
              {usage.estimator}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
