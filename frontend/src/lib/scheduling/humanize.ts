import type { ScheduledTask } from '../api/types'

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

export function humanizeSchedule(task: Pick<ScheduledTask, 'scheduleKind' | 'cronExpr' | 'intervalSeconds' | 'runAt' | 'timezone'>): string {
  if (task.scheduleKind === 'cron') return humanizeCron(task.cronExpr)
  if (task.scheduleKind === 'interval') return humanizeInterval(task.intervalSeconds)
  return humanizeOnce(task.runAt)
}

export function humanizeCron(expr: string): string {
  const parts = expr.trim().split(/\s+/)
  if (parts.length !== 5) return expr || 'Cron'
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts
  const time = formatCronTime(hour, minute)

  if (minute.startsWith('*/') && hour === '*' && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
    return `Every ${minute.slice(2)}m`
  }
  if (minute === '0' && hour.startsWith('*/') && dayOfMonth === '*' && month === '*' && dayOfWeek === '*') {
    return `Every ${hour.slice(2)}h`
  }
  if (dayOfMonth === '*' && month === '*' && dayOfWeek === '1-5') return `Weekdays ${time}`
  if (dayOfMonth === '*' && month === '*' && /^\d$/.test(dayOfWeek)) {
    return `${WEEKDAYS[Number(dayOfWeek)] || 'Day'} ${time}`
  }
  if (dayOfMonth !== '*' && month === '*' && dayOfWeek === '*') return `Monthly day ${dayOfMonth} ${time}`
  if (dayOfMonth === '*' && month === '*' && dayOfWeek === '*') return `Daily ${time}`
  return expr
}

export function humanizeInterval(seconds: number): string {
  const value = Math.max(0, Number(seconds) || 0)
  if (value <= 0) return 'Interval'
  if (value % 86400 === 0) return `Every ${compactUnit(value / 86400, 'd')}`
  if (value % 3600 === 0) return `Every ${compactUnit(value / 3600, 'h')}`
  if (value % 60 === 0) return `Every ${compactUnit(value / 60, 'm')}`
  return `Every ${compactUnit(value, 's')}`
}

export function humanizeOnce(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Once'
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

export function relativeTime(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const diffMs = date.getTime() - Date.now()
  const past = diffMs < 0
  const absMins = Math.max(0, Math.round(Math.abs(diffMs) / 60_000))
  if (absMins < 1) return past ? 'just now' : 'now'
  if (absMins < 60) return past ? `${absMins}m ago` : `in ${absMins}m`
  const hours = Math.round(absMins / 60)
  if (hours < 24) return past ? `${hours}h ago` : `in ${hours}h`
  const days = Math.round(hours / 24)
  return past ? `${days}d ago` : `in ${days}d`
}

function compactUnit(value: number, unit: string): string {
  return `${value}${unit}`
}

function formatCronTime(hour: string, minute: string): string {
  if (!/^\d+$/.test(hour) || !/^\d+$/.test(minute)) return `${hour}:${minute}`
  return `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
}
