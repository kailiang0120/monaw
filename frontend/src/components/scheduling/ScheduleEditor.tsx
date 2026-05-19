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

// ─── cron field definitions ───────────────────────────────────────────────────
const CRON_FIELDS = [
  { label: 'Minute',  placeholder: '0',   hint: '0 – 59'       },
  { label: 'Hour',    placeholder: '8',   hint: '0 – 23'       },
  { label: 'Day',     placeholder: '*',   hint: '1 – 31'       },
  { label: 'Month',   placeholder: '*',   hint: '1 – 12'       },
  { label: 'Weekday', placeholder: '1-5', hint: '0=Sun  6=Sat' },
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
  const interval = secondsToInterval(intervalSeconds)
  const cronParts = splitCron(cronExpr)

  const adjustValue = (delta: number) => {
    const next = Math.max(1, interval.value + delta)
    onIntervalSecondsChange(intervalToSeconds(next, interval.unit))
  }

  const updateCronField = (index: number, raw: string) => {
    const parts = splitCron(cronExpr)
    // Allow empty while editing; use '*' as placeholder only when empty on blur
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
      {/* ── Kind selector ────────────────────────────────── */}
      <div className="grid grid-cols-3 gap-1 rounded-lg border border-white/[0.08] bg-black/10 p-1">
        {(
          [
            ['cron', 'Cron'],
            ['interval', 'Every'],
            ['once', 'Once'],
          ] as const
        ).map(([kind, label]) => (
          <button
            key={kind}
            type="button"
            onClick={() => onScheduleKindChange(kind)}
            className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              scheduleKind === kind
                ? 'bg-accent text-white shadow-sm'
                : 'text-neutral-500 hover:bg-white/[0.04] hover:text-neutral-200'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* ── Cron — 5 labeled fields ───────────────────────── */}
      {scheduleKind === 'cron' && (
        <div className="space-y-3">
          {/* 5-column grid */}
          <div className="grid grid-cols-5 gap-2">
            {CRON_FIELDS.map(({ label, placeholder, hint }, i) => (
              <div key={label} className="space-y-1">
                {/* Field label */}
                <p className="text-center text-[10px] font-semibold uppercase tracking-wide text-neutral-500">
                  {label}
                </p>
                {/* Input */}
                <input
                  value={cronParts[i]}
                  onChange={(e) => updateCronField(i, e.target.value)}
                  onBlur={() => finalizeCronField(i)}
                  className="control w-full rounded-lg px-1.5 py-1.5 text-center font-mono text-sm"
                  placeholder={placeholder}
                  spellCheck={false}
                  autoComplete="off"
                />
                {/* Range hint */}
                <p className="text-center text-[9px] leading-tight text-neutral-600">{hint}</p>
              </div>
            ))}
          </div>

          <CronHelper cronExpr={cronExpr} timezone={timezone} onChange={onCronExprChange} />
        </div>
      )}

      {/* ── Interval ─────────────────────────────────────── */}
      {scheduleKind === 'interval' && (
        <div className="flex items-center gap-2">
          {/* Number stepper */}
          <div className="flex items-center overflow-hidden rounded-lg border border-white/[0.1] bg-white/[0.035]">
            <button
              type="button"
              onClick={() => adjustValue(-1)}
              aria-label="Decrease"
              className="flex h-9 w-8 items-center justify-center text-base text-neutral-500 transition-colors hover:bg-white/[0.06] hover:text-neutral-100"
            >
              −
            </button>
            <input
              type="number"
              min={1}
              value={interval.value}
              onChange={(e) => {
                const v = Math.max(1, +e.target.value || 1)
                onIntervalSecondsChange(intervalToSeconds(v, interval.unit))
              }}
              className="w-14 bg-transparent py-1.5 text-center text-sm text-neutral-200 outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            />
            <button
              type="button"
              onClick={() => adjustValue(1)}
              aria-label="Increase"
              className="flex h-9 w-8 items-center justify-center text-base text-neutral-500 transition-colors hover:bg-white/[0.06] hover:text-neutral-100"
            >
              +
            </button>
          </div>

          {/* Unit pill selector */}
          <div className="flex flex-1 gap-1 rounded-lg border border-white/[0.08] bg-black/10 p-1">
            {UNITS.map((unit) => (
              <button
                key={unit}
                type="button"
                onClick={() =>
                  onIntervalSecondsChange(intervalToSeconds(interval.value, unit))
                }
                className={`flex-1 rounded-md py-1.5 text-xs font-medium transition-colors ${
                  interval.unit === unit
                    ? 'bg-accent text-white shadow-sm'
                    : 'text-neutral-500 hover:bg-white/[0.04] hover:text-neutral-200'
                }`}
              >
                {unit}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Once ─────────────────────────────────────────── */}
      {scheduleKind === 'once' && (
        <DateTimePicker value={runAt} onChange={onRunAtChange} />
      )}

      {/* ── Timezone (cron + once only) ───────────────────── */}
      {scheduleKind !== 'interval' && (
        <TimezoneSelect value={timezone} onChange={onTimezoneChange} />
      )}
    </div>
  )
}

// ─── helpers ──────────────────────────────────────────────────────────────────
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
