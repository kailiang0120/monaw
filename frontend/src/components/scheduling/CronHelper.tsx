import { useEffect, useState, type ReactNode } from 'react'
import { previewSchedule } from '../../lib/api/scheduledTasks'
import { humanizeCron } from '../../lib/scheduling/humanize'

const PRESETS = [
  { label: 'Every 15 minutes', value: '*/15 * * * *' },
  { label: 'Every 30 minutes', value: '*/30 * * * *' },
  { label: 'Every hour', value: '0 * * * *' },
  { label: 'Every day at 8am', value: '0 8 * * *' },
  { label: 'Weekdays at 8am', value: '0 8 * * 1-5' },
  { label: 'Mondays at 9am', value: '0 9 * * 1' },
]

interface Props {
  cronExpr: string
  timezone: string
  onChange: (value: string) => void
  /** Rendered between the presets and the preview — used for the raw editor. */
  children?: ReactNode
}

export function CronHelper({ cronExpr, timezone, onChange, children }: Props) {
  const [preview, setPreview] = useState<string[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Human-readable interpretation, shown only when it adds something.
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
    <div className="space-y-3">
      <div>
        <p className="st-hint mb-1.5">Start from a common schedule</p>
        <div className="flex flex-wrap gap-1.5">
          {PRESETS.map((preset) => {
            const active = cronExpr.trim() === preset.value
            return (
              <button
                key={preset.value}
                type="button"
                onClick={() => onChange(preset.value)}
                aria-pressed={active}
                className={`st-badge ${active ? 'st-badge-accent' : ''} cursor-pointer`}
              >
                {preset.label}
              </button>
            )
          })}
        </div>
      </div>

      {children}

      <div className="st-note">
        <div className="mb-1.5 flex items-center gap-2">
          <span className="st-group-label">Next runs</span>
          {loading && <span className="st-dot animate-pulse" />}
        </div>
        {error ? (
          <p style={{ color: 'var(--st-danger)' }}>{error}</p>
        ) : preview.length === 0 ? (
          <p>Pick a schedule above to see when this will run.</p>
        ) : (
          <>
            {humanized && (
              <p className="mb-1.5" style={{ color: 'var(--st-accent-text)' }}>{humanized}</p>
            )}
            <div className="space-y-0.5">
              {preview.map((value, index) => (
                <p
                  key={value}
                  className="tabular-nums"
                  style={{ color: index === 0 ? 'var(--st-text)' : undefined }}
                >
                  {new Date(value).toLocaleString([], {
                    weekday: 'short',
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </p>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
