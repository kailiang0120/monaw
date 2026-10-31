import { useEffect, useRef, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { cleanStorage, fetchStorageUsage } from '../../lib/api/settings'
import type { StorageCategory, StorageUsage } from '../../lib/api/types'
import { formatBytes } from '../../lib/formatBytes'
import { Note, SettingsCard } from '../../components/ui/Panel'

// One colour per category, readable on both themes; "other" stays grey.
const CATEGORY_COLORS: Record<string, string> = {
  chats: '#6f9e3c',
  uploads: '#3f8f9b',
  activity: '#c08a2e',
  logs: '#a8743f',
  outputs: '#8a6fb8',
  browser: '#4c7fc0',
  app_cache: '#5fa3d6',
  legacy_speech: '#c06a5a',
  memory: '#b45f95',
  account: '#7d8c3a',
  workspace: '#3f9b7a',
  other: '#8b887f',
}

function color(id: string): string {
  return CATEGORY_COLORS[id] ?? CATEGORY_COLORS.other
}

export function StorageSettings() {
  const [usage, setUsage] = useState<StorageUsage | null>(null)
  const [loading, setLoading] = useState(true)
  const [cleaning, setCleaning] = useState('')
  // Shown under the row that was cleaned, which may be far below the total.
  const [notice, setNotice] = useState<{ id: string; text: string } | null>(null)
  const [error, setError] = useState('')
  const pending = useRef<AbortController | null>(null)

  const load = async () => {
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    setLoading(true)
    setError('')
    try {
      const result = await fetchStorageUsage(controller.signal)
      if (!controller.signal.aborted) setUsage(result)
    } catch (exc) {
      if (!controller.signal.aborted) setError(exc instanceof Error ? exc.message : 'Storage could not be measured.')
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    return () => pending.current?.abort()
  }, [])

  const clean = async (category: StorageCategory) => {
    if (category.warning && !window.confirm(`${category.clean_label}? ${category.warning}`)) return
    setCleaning(category.id)
    setNotice(null)
    setError('')
    try {
      if (category.clean === 'app') {
        if (!window.electronAPI?.clearAppCache) throw new Error('The app cache can only be cleared from the Monaw app.')
        await window.electronAPI.clearAppCache()
        const before = category.bytes
        const next = await fetchStorageUsage()
        const after = next.categories.find((item) => item.id === category.id)?.bytes ?? 0
        setUsage(next)
        setNotice({ id: category.id, text: `Freed ${formatBytes(Math.max(0, before - after))} from ${category.label.toLowerCase()}.` })
      } else {
        const result = await cleanStorage(category.id)
        setUsage(result.usage)
        setNotice({ id: category.id, text: `Freed ${formatBytes(result.freed_bytes)} from ${category.label.toLowerCase()}.` })
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Cleanup failed.')
    } finally {
      setCleaning('')
    }
  }

  const categories = usage?.categories ?? []
  const shown = categories.filter((item) => item.bytes > 0 || item.clean)
  const cleanable = categories.reduce((total, item) => total + item.cleanable_bytes, 0)

  return (
    <div className="space-y-4">
      <SettingsCard
        title="Storage used"
        action={
          <button type="button" className="st-btn st-btn-secondary st-btn-icon" onClick={() => void load()} disabled={loading || Boolean(cleaning)} aria-label="Measure again" title="Measure again">
            {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
          </button>
        }
      >
        <div className="space-y-3 px-4 py-4">
          {usage ? (
            <>
              <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="text-2xl font-semibold" style={{ color: 'var(--st-text)' }}>{formatBytes(usage.total_bytes)}</span>
                <span className="st-desc">{formatBytes(cleanable)} can be cleaned</span>
              </p>
              <div className="flex h-2.5 w-full overflow-hidden rounded-full" style={{ background: 'var(--st-surface-hover)' }} role="img" aria-label="Storage by category">
                {categories.filter((item) => item.bytes > 0).map((item) => (
                  <span
                    key={item.id}
                    title={`${item.label}: ${formatBytes(item.bytes)}`}
                    style={{ width: `${(item.bytes / Math.max(1, usage.total_bytes)) * 100}%`, background: color(item.id) }}
                  />
                ))}
              </div>
              <p className="st-desc truncate" title={usage.location}>{usage.location}</p>
            </>
          ) : (
            <p className="st-desc">{loading ? 'Measuring Monaw’s folders…' : 'Storage could not be measured.'}</p>
          )}
          {error && <Note tone="danger">{error}</Note>}
        </div>
      </SettingsCard>

      {usage && (
        <SettingsCard title="What uses it">
          {shown.map((item) => (
            <div key={item.id} className="st-row">
              <div className="min-w-0">
                <p className="st-label flex items-center gap-2">
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: color(item.id) }} aria-hidden="true" />
                  {item.label}
                  <span className="st-desc font-normal">{formatBytes(item.bytes)}</span>
                </p>
                <p className="st-desc mt-1">{item.description}</p>
                {notice?.id === item.id && <p role="status" className="st-desc mt-1" style={{ color: 'var(--st-accent-text)' }}>{notice.text}</p>}
              </div>
              {item.clean && (
                <div className="st-row-control-auto">
                  <button
                    type="button"
                    className={`st-btn ${item.warning ? 'st-btn-danger' : 'st-btn-secondary'}`}
                    onClick={() => void clean(item)}
                    disabled={Boolean(cleaning) || loading || item.cleanable_bytes <= 0}
                  >
                    {cleaning === item.id && <Loader2 size={13} className="animate-spin" />}
                    {item.clean_label}
                  </button>
                </div>
              )}
            </div>
          ))}
        </SettingsCard>
      )}
    </div>
  )
}
