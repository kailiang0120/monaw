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
        className="control flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm"
      >
        <Globe size={13} className="shrink-0 text-neutral-500" />
        <span className="min-w-0 flex-1 truncate text-left text-neutral-300">{value}</span>
        <span className="shrink-0 font-mono text-[11px] text-neutral-500">{currentOffset}</span>
        <ChevronDown
          size={13}
          className={`shrink-0 text-neutral-500 transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Dropdown — fixed position so it escapes overflow:hidden */}
      {open && dropPos && (
        <div
          id="tz-dropdown"
          style={{ position: 'fixed', top: dropPos.top, left: dropPos.left, width: dropPos.width }}
          className="z-[200] overflow-hidden rounded-xl border border-white/[0.1] bg-[#141312] shadow-2xl"
        >
          {/* Search */}
          <div className="border-b border-white/[0.07] px-3 py-2">
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search timezone…"
              className="w-full bg-transparent text-sm text-neutral-200 placeholder-neutral-600 outline-none"
              onKeyDown={(e) => e.key === 'Escape' && closeDropdown()}
            />
          </div>

          {/* List */}
          <div className="max-h-52 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <p className="px-4 py-3 text-xs text-neutral-500">No timezones found</p>
            ) : (
              filtered.map((tz) => (
                <button
                  key={tz}
                  type="button"
                  onClick={() => {
                    onChange(tz)
                    closeDropdown()
                  }}
                  className={`flex w-full items-center gap-3 px-3 py-1.5 text-left transition-colors hover:bg-white/[0.05] ${
                    tz === value ? 'bg-accent/10 text-accent-light' : 'text-neutral-300'
                  }`}
                >
                  <span className="w-[4.5rem] shrink-0 font-mono text-[10px] text-neutral-500">
                    {getOffset(tz)}
                  </span>
                  <span className="text-xs">{tz}</span>
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  )
}
