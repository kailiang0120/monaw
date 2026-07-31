import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Globe } from 'lucide-react'

// ─── timezone list ────────────────────────────────────────────────────────────
const ALL_TZ = (() => {
  const fallback = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  const intl = Intl as typeof Intl & { supportedValuesOf?: (k: string) => string[] }
  const list =
    typeof intl.supportedValuesOf === 'function'
      ? intl.supportedValuesOf('timeZone')
      : [fallback]
  return list.includes(fallback) ? list : [fallback, ...list]
})()

// ─── UTC offset helpers ───────────────────────────────────────────────────────
const _offsetCache = new Map<string, string>()

function getOffset(tz: string): string {
  if (_offsetCache.has(tz)) return _offsetCache.get(tz)!
  try {
    const str = new Intl.DateTimeFormat('en', {
      timeZone: tz,
      timeZoneName: 'shortOffset',
    }).format(new Date())
    const m = str.match(/GMT([+-]\d+(?::\d+)?|0)/i)
    const v = m ? `UTC${m[1] === '0' ? '+0' : m[1]}` : 'UTC'
    _offsetCache.set(tz, v)
    return v
  } catch {
    _offsetCache.set(tz, 'UTC')
    return 'UTC'
  }
}

// ─── component ────────────────────────────────────────────────────────────────
interface Props {
  value: string
  onChange: (v: string) => void
}

export function TimezoneSelect({ value, onChange }: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const [dropPos, setDropPos] = useState<{ top: number; left: number; width: number } | null>(null)

  const filtered = query.trim()
    ? ALL_TZ.filter((tz) => tz.toLowerCase().includes(query.toLowerCase())).slice(0, 80)
    : ALL_TZ

  const openDropdown = () => {
    const rect = containerRef.current?.getBoundingClientRect()
    if (rect) setDropPos({ top: rect.bottom + 4, left: rect.left, width: rect.width })
    setOpen(true)
  }

  const closeDropdown = () => {
    setOpen(false)
    setQuery('')
    setDropPos(null)
  }

  useEffect(() => {
    if (!open) return
    const id = setTimeout(() => searchRef.current?.focus(), 0)
    const handler = (e: MouseEvent) => {
      const el = e.target as Node
      const inside =
        containerRef.current?.contains(el) ||
        document.getElementById('tz-dropdown')?.contains(el)
      if (!inside) closeDropdown()
    }
    document.addEventListener('mousedown', handler)
    return () => {
      clearTimeout(id)
      document.removeEventListener('mousedown', handler)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const currentOffset = getOffset(value)

  return (
    <div ref={containerRef} className="relative">
      {/* Trigger */}
      <button
        type="button"
        onClick={open ? closeDropdown : openDropdown}
        aria-label="Timezone"
        aria-expanded={open}
        className="st-select gap-2"
      >
        <Globe size={13} className="st-nav-icon" />
        <span className="min-w-0 flex-1 truncate text-left">{value}</span>
        <span className="st-hint shrink-0 font-mono">{currentOffset}</span>
        <ChevronDown
          size={13}
          className={`st-nav-icon transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Dropdown — fixed position so it escapes overflow:hidden */}
      {open && dropPos && (
        <div
          id="tz-dropdown"
          style={{ position: 'fixed', top: dropPos.top, left: dropPos.left, width: dropPos.width }}
          className="st-menu z-[200] overflow-hidden p-0"
        >
          <div className="st-divider-b px-3 py-2">
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search timezone…"
              aria-label="Search timezone"
              className="w-full border-0 bg-transparent text-sm outline-none"
              style={{ color: 'var(--st-text)' }}
              onKeyDown={(e) => e.key === 'Escape' && closeDropdown()}
            />
          </div>

          <div className="max-h-52 overflow-y-auto p-1">
            {filtered.length === 0 ? (
              <p className="st-hint px-3 py-3">No timezones found</p>
            ) : (
              filtered.map((tz) => (
                <button
                  key={tz}
                  type="button"
                  aria-selected={tz === value}
                  onClick={() => {
                    onChange(tz)
                    closeDropdown()
                  }}
                  className="st-menu-item justify-start gap-3"
                >
                  <span className="w-[4.5rem] shrink-0 font-mono text-[0.625rem]">{getOffset(tz)}</span>
                  <span className="truncate text-xs">{tz}</span>
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  )
}
