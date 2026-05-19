import { useEffect, useRef, useState } from 'react'
import { Calendar, ChevronLeft, ChevronRight } from 'lucide-react'

// ─── constants ────────────────────────────────────────────────────────────────
const DAYS_ABBR = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa']
const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

// ─── helpers ──────────────────────────────────────────────────────────────────
interface DateSel {
  y: number
  mo: number
  d: number
}

function parse(v: string): (DateSel & { h: number; mi: number }) | null {
  const m = v.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/)
  if (!m) return null
  return { y: +m[1], mo: +m[2], d: +m[3], h: +m[4], mi: +m[5] }
}

function fmt(y: number, mo: number, d: number, h: number, mi: number): string {
  return (
    `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}` +
    `T${String(h).padStart(2, '0')}:${String(mi).padStart(2, '0')}`
  )
}

function daysInMonth(y: number, mo: number) {
  return new Date(y, mo, 0).getDate()
}
function firstWeekDay(y: number, mo: number) {
  return new Date(y, mo - 1, 1).getDay()
}

// ─── component ────────────────────────────────────────────────────────────────
interface Props {
  value: string // "YYYY-MM-DDTHH:mm" or ""
  onChange: (v: string) => void
}

export function DateTimePicker({ value, onChange }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [dropPos, setDropPos] = useState<{ top: number; left: number } | null>(null)

  const today = new Date()
  const initial = parse(value)

  const [vy, setVy] = useState(initial?.y ?? today.getFullYear()) // view year
  const [vm, setVm] = useState(initial?.mo ?? today.getMonth() + 1) // view month
  const [sel, setSel] = useState<DateSel | null>(
    initial ? { y: initial.y, mo: initial.mo, d: initial.d } : null,
  )
  const [h, setH] = useState(initial?.h ?? 9)
  const [mi, setMi] = useState(initial?.mi ?? 0)

  // Sync internal selection when value changes externally
  useEffect(() => {
    const p = parse(value)
    if (p) {
      setSel({ y: p.y, mo: p.mo, d: p.d })
      setH(p.h)
      setMi(p.mi)
    } else {
      setSel(null)
    }
  }, [value])

  // Click-outside to close
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      const insideRef = ref.current?.contains(target)
      const insideDrop = document.getElementById('dtp-dropdown')?.contains(target)
      if (!insideRef && !insideDrop) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const openPicker = () => {
    const rect = ref.current?.getBoundingClientRect()
    if (rect) setDropPos({ top: rect.bottom + 4, left: rect.left })
    setOpen(true)
  }

  const prevMonth = () =>
    vm === 1 ? (setVm(12), setVy((y) => y - 1)) : setVm((m) => m - 1)
  const nextMonth = () =>
    vm === 12 ? (setVm(1), setVy((y) => y + 1)) : setVm((m) => m + 1)

  const pickDay = (d: number) => {
    const next: DateSel = { y: vy, mo: vm, d }
    setSel(next)
    onChange(fmt(next.y, next.mo, next.d, h, mi))
  }

  const updateTime = (newH: number, newMi: number) => {
    const ch = Math.min(23, Math.max(0, newH))
    const cm = Math.min(59, Math.max(0, newMi))
    setH(ch)
    setMi(cm)
    if (sel) onChange(fmt(sel.y, sel.mo, sel.d, ch, cm))
  }

  // Display string for the trigger
  const display = value
    ? (() => {
        try {
          return new Date(`${value}:00`).toLocaleString([], {
            month: 'short',
            day: 'numeric',
            year: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
          })
        } catch {
          return value
        }
      })()
    : null

  const dim = daysInMonth(vy, vm)
  const firstDay = firstWeekDay(vy, vm)

  return (
    <div ref={ref} className="relative">
      {/* Trigger */}
      <button
        type="button"
        onClick={open ? () => setOpen(false) : openPicker}
        className="control flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm"
      >
        <Calendar size={13} className="shrink-0 text-neutral-500" />
        <span className={`flex-1 text-left ${display ? 'text-neutral-200' : 'text-neutral-600'}`}>
          {display ?? 'Pick date and time…'}
        </span>
      </button>

      {/* Dropdown — fixed-positioned to escape overflow:hidden on dialog panel */}
      {open && dropPos && (
        <div
          id="dtp-dropdown"
          style={{ position: 'fixed', top: dropPos.top, left: dropPos.left, width: 272 }}
          className="z-[200] rounded-xl border border-white/[0.1] bg-[#141312] p-3 shadow-2xl"
        >
          {/* Month navigation */}
          <div className="mb-3 flex items-center justify-between">
            <button
              type="button"
              onClick={prevMonth}
              className="ghost-button h-7 w-7 rounded-md"
              aria-label="Previous month"
            >
              <ChevronLeft size={14} />
            </button>
            <span className="text-xs font-semibold text-neutral-100">
              {MONTH_NAMES[vm - 1]} {vy}
            </span>
            <button
              type="button"
              onClick={nextMonth}
              className="ghost-button h-7 w-7 rounded-md"
              aria-label="Next month"
            >
              <ChevronRight size={14} />
            </button>
          </div>

          {/* Weekday headers */}
          <div className="mb-1 grid grid-cols-7">
            {DAYS_ABBR.map((d) => (
              <div
                key={d}
                className="flex h-6 items-center justify-center text-[10px] font-medium text-neutral-600"
              >
                {d}
              </div>
            ))}
          </div>

          {/* Day grid */}
          <div className="grid grid-cols-7 gap-y-0.5">
            {/* Empty cells for first-of-month offset */}
            {Array.from({ length: firstDay }).map((_, i) => (
              <div key={`e${i}`} />
            ))}

            {Array.from({ length: dim }).map((_, i) => {
              const d = i + 1
              const isSel = sel?.d === d && sel.mo === vm && sel.y === vy
              const isToday =
                d === today.getDate() &&
                vm === today.getMonth() + 1 &&
                vy === today.getFullYear()
              return (
                <button
                  key={d}
                  type="button"
                  onClick={() => pickDay(d)}
                  className={`flex h-7 w-full items-center justify-center rounded-md text-xs font-medium transition-colors ${
                    isSel
                      ? 'bg-accent text-white shadow-sm'
                      : isToday
                        ? 'text-accent-light ring-1 ring-accent/50'
                        : 'text-neutral-400 hover:bg-white/[0.07] hover:text-neutral-100'
                  }`}
                >
                  {d}
                </button>
              )
            })}
          </div>

          {/* Time picker */}
          <div className="mt-3 flex items-center gap-2 border-t border-white/[0.07] pt-3">
            <span className="shrink-0 text-[11px] text-neutral-500">Time</span>
            <div className="flex items-center gap-1">
              <input
                type="number"
                min={0}
                max={23}
                value={String(h).padStart(2, '0')}
                onChange={(e) => updateTime(+e.target.value, mi)}
                className="control w-11 rounded-md px-1 py-1 text-center text-sm [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                aria-label="Hour"
              />
              <span className="font-semibold text-neutral-500">:</span>
              <input
                type="number"
                min={0}
                max={59}
                step={5}
                value={String(mi).padStart(2, '0')}
                onChange={(e) => updateTime(h, +e.target.value)}
                className="control w-11 rounded-md px-1 py-1 text-center text-sm [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                aria-label="Minute"
              />
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="ml-auto rounded-lg bg-accent/20 px-3 py-1 text-xs font-medium text-accent-light transition-colors hover:bg-accent/30"
            >
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
