import { useEffect, useState } from 'react'
import { previewSchedule } from '../../lib/api/scheduledTasks'
import { humanizeCron } from '../../lib/scheduling/humanize'

const PRESETS = [
  { label: 'Every 15m',   value: '*/15 * * * *' },
  { label: 'Every 30m',   value: '*/30 * * * *' },
  { label: 'Hourly',      value: '0 * * * *' },
  { label: 'Daily 8am',   value: '0 8 * * *' },
  { label: 'Weekdays 8am',value: '0 8 * * 1-5' },
  { label: 'Mon 9am',     value: '0 9 * * 1' },
]

interface Props {
  cronExpr: string
  timezone: string
  onChange: (value: string) => void
}

export function CronHelper({ cronExpr, timezone, onChange }: Props) {
  const [preview, setPreview] = useState<string[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Human-readable interpretation (only show when different from raw expr)
  const humanized = (() => {
    if (!cronExpr.trim()) return ''
    try {
      const h = humanizeCron(cronExpr)
      return h !== cronExpr ? h : ''
    } catch {
      return ''
    }
  })()

  useEffect(() => {
    if (!cronExpr.trim()) {
      setPreview([])
      setError('')
      return
    }
    setLoading(true)
    let cancelled = false
    const id = window.setTimeout(async () => {
      try {
        const result = await previewSchedule({ scheduleKind: 'cron', cronExpr, timezone })
        if (!cancelled) {
          setPreview(result.next)
          setError('')
          setLoading(false)
        }
      } catch (err) {
        if (!cancelled) {
          setPreview([])
          setError(err instanceof Error ? err.message : 'Invalid expression')
          setLoading(false)
        }
      }
    }, 300)
    return () => {
      cancelled = true
      window.clearTimeout(id)
    }
  }, [cronExpr, timezone])

  return (
    <div className="space-y-2.5">
      {/* Preset chips */}
      <div className="flex flex-wrap gap-1.5">
        {PRESETS.map((p) => {
          const active = cronExpr.trim() === p.value
          return (
            <button
              key={p.value}
              type="button"
              onClick={() => onChange(p.value)}
              className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-all ${
                active
                  ? 'border-accent/60 bg-accent/15 text-accent-light'
                  : 'border-white/[0.08] bg-white/[0.03] text-neutral-400 hover:border-white/[0.18] hover:text-neutral-100'
              }`}
            >
              {p.label}
            </button>
          )
        })}
      </div>

      {/* Human-readable interpretation badge */}
      {humanized && (
        <div className="flex items-center gap-2 rounded-lg border border-accent/25 bg-accent/10 px-3 py-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-neutral-500">Means</span>
          <span className="text-xs font-medium text-accent-light">{humanized}</span>
        </div>
      )}

      {/* Next-fires preview panel */}
      <div className="rounded-lg border border-white/[0.07] bg-black/10 px-3 py-2.5">
        <div className="mb-1.5 flex items-center gap-2">
          <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-neutral-600">
            Next fires
          </p>
          {loading && (
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent/60" />
          )}
        </div>

        {error ? (
          <p className="text-xs text-red-400">{error}</p>
        ) : preview.length === 0 ? (
          <p className="text-xs text-neutral-600">Enter a valid cron expression above.</p>
        ) : (
          <div className="space-y-1">
            {preview.map((v, i) => (
              <p
                key={v}
                className={`text-xs tabular-nums ${i === 0 ? 'text-neutral-300' : 'text-neutral-500'}`}
              >
                {new Date(v).toLocaleString([], {
                  weekday: 'short',
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </p>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
