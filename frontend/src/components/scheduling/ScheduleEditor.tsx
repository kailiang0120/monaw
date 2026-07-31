import { useState } from 'react'
import { ChevronDown } from 'lucide-react'
import type { ScheduleKind } from '../../lib/api/types'
import { CronHelper } from './CronHelper'
import { DateTimePicker } from './DateTimePicker'
import { TimezoneSelect } from './TimezoneSelect'

interface Props {
  scheduleKind: ScheduleKind
  cronExpr: string
  intervalSeconds: number
  runAt: string
  timezone: string
  onScheduleKindChange: (value: ScheduleKind) => void
  onCronExprChange: (value: string) => void
  onIntervalSecondsChange: (value: number) => void
  onRunAtChange: (value: string) => void
  onTimezoneChange: (value: string) => void
}

const UNITS = ['minutes', 'hours', 'days'] as const
type Unit = (typeof UNITS)[number]

/**
 * "Cron" means nothing to most people, so the modes are named after what they
 * do. The cron expression itself is still available, one disclosure down.
 */
const KINDS: Array<{ kind: ScheduleKind; label: string; hint: string }> = [
  { kind: 'cron', label: 'At set times', hint: 'Weekdays at 8am, the 1st of every month, and so on.' },
  { kind: 'interval', label: 'Every so often', hint: 'A fixed gap between runs, starting from when you save.' },
  { kind: 'once', label: 'Once', hint: 'A single run at a date and time you choose.' },
]

const CRON_FIELDS = [
  { label: 'Minute', placeholder: '0', hint: '0–59' },
  { label: 'Hour', placeholder: '8', hint: '0–23' },
  { label: 'Day', placeholder: '*', hint: '1–31' },
  { label: 'Month', placeholder: '*', hint: '1–12' },
  { label: 'Weekday', placeholder: '1-5', hint: '0=Sun, 6=Sat' },
] as const

/** Split a cron string into exactly 5 parts, filling with '*' if needed. */
function splitCron(expr: string): [string, string, string, string, string] {
  const parts = expr.trim().split(/\s+/)
  const padded = [...parts, '*', '*', '*', '*', '*'].slice(0, 5)
  return padded as [string, string, string, string, string]
}

export function ScheduleEditor({
  scheduleKind,
  cronExpr,
  intervalSeconds,
  runAt,
  timezone,
  onScheduleKindChange,
  onCronExprChange,
  onIntervalSecondsChange,
  onRunAtChange,
  onTimezoneChange,
}: Props) {
  const [rawCronOpen, setRawCronOpen] = useState(false)
  const interval = secondsToInterval(intervalSeconds)
  const cronParts = splitCron(cronExpr)
  const activeKind = KINDS.find((item) => item.kind === scheduleKind)

  const adjustValue = (delta: number) => {
    const next = Math.max(1, interval.value + delta)
    onIntervalSecondsChange(intervalToSeconds(next, interval.unit))
  }

  const updateCronField = (index: number, raw: string) => {
    const parts = splitCron(cronExpr)
    parts[index] = raw
    onCronExprChange(parts.join(' '))
  }

  const finalizeCronField = (index: number) => {
    const parts = splitCron(cronExpr)
    if (!parts[index].trim()) parts[index] = '*'
    onCronExprChange(parts.join(' '))
  }

  return (
    <div className="space-y-3">
      <div className="st-segment w-full">
        {KINDS.map(({ kind, label }) => (
          <button
            key={kind}
            type="button"
            onClick={() => onScheduleKindChange(kind)}
            aria-pressed={scheduleKind === kind}
            className="st-segment-item flex-1"
          >
            {label}
          </button>
        ))}
      </div>
      {activeKind && <p className="st-desc">{activeKind.hint}</p>}

      {scheduleKind === 'cron' && (
        <CronHelper cronExpr={cronExpr} timezone={timezone} onChange={onCronExprChange}>
          <div>
            <button
              type="button"
              onClick={() => setRawCronOpen((value) => !value)}
              className="st-btn st-btn-ghost -ml-2"
            >
              <ChevronDown size={12} className={`transition-transform ${rawCronOpen ? 'rotate-180' : ''}`} />
              {rawCronOpen ? 'Hide custom expression' : 'Write a custom expression'}
            </button>
            {rawCronOpen && (
              <div className="mt-2 grid grid-cols-5 gap-2">
                {CRON_FIELDS.map(({ label, placeholder, hint }, index) => (
                  <div key={label} className="space-y-1">
                    <p className="st-hint text-center">{label}</p>
                    <input
                      value={cronParts[index]}
                      aria-label={`Cron ${label.toLowerCase()}`}
                      onChange={(event) => updateCronField(index, event.target.value)}
                      onBlur={() => finalizeCronField(index)}
                      className="st-input text-center font-mono"
                      placeholder={placeholder}
                      spellCheck={false}
                      autoComplete="off"
                    />
                    <p className="st-hint text-center text-[0.625rem]">{hint}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </CronHelper>
      )}

      {scheduleKind === 'interval' && (
        <div className="flex flex-wrap items-center gap-2">
          <div
            className="flex items-center overflow-hidden rounded-lg"
            style={{ border: '1px solid var(--st-border-strong)', background: 'var(--st-surface-sunken)' }}
          >
            <button
              type="button"
              onClick={() => adjustValue(-1)}
              aria-label="Decrease interval"
              className="st-btn st-btn-ghost h-9 w-9 rounded-none"
            >
              −
            </button>
            <input
              type="number"
              min={1}
              aria-label="Interval value"
              value={interval.value}
              onChange={(event) => {
                const next = Math.max(1, Number(event.target.value) || 1)
                onIntervalSecondsChange(intervalToSeconds(next, interval.unit))
              }}
              className="w-14 border-0 bg-transparent py-1.5 text-center text-sm outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
              style={{ color: 'var(--st-text)' }}
            />
            <button
              type="button"
              onClick={() => adjustValue(1)}
              aria-label="Increase interval"
              className="st-btn st-btn-ghost h-9 w-9 rounded-none"
            >
              +
            </button>
          </div>

          <div className="st-segment flex-1">
            {UNITS.map((unit) => (
              <button
                key={unit}
                type="button"
                onClick={() => onIntervalSecondsChange(intervalToSeconds(interval.value, unit))}
                aria-pressed={interval.unit === unit}
                className="st-segment-item flex-1 capitalize"
              >
                {unit}
              </button>
            ))}
          </div>
        </div>
      )}

      {scheduleKind === 'once' && <DateTimePicker value={runAt} onChange={onRunAtChange} />}

      {scheduleKind !== 'interval' && (
        <div>
          <p className="st-hint mb-1.5">Times are interpreted in this timezone</p>
          <TimezoneSelect value={timezone} onChange={onTimezoneChange} />
        </div>
      )}
    </div>
  )
}

function secondsToInterval(seconds: number): { value: number; unit: Unit } {
  const n = Math.max(60, Number(seconds) || 3600)
  if (n % 86400 === 0) return { value: n / 86400, unit: 'days' }
  if (n % 3600 === 0) return { value: n / 3600, unit: 'hours' }
  return { value: Math.max(1, Math.round(n / 60)), unit: 'minutes' }
}

function intervalToSeconds(value: number, unit: string): number {
  if (unit === 'days') return value * 86400
  if (unit === 'hours') return value * 3600
  return value * 60
}
