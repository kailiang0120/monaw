import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Archive, CheckCircle2, FileText, Plus, RotateCcw, Save, Search, Settings2, Trash2 } from 'lucide-react'
import {
  createMemory,
  deleteMemory,
  fetchMemories,
  fetchMemoryFile,
  fetchMemoryStats,
  saveMemoryFile,
  updateMemory,
  updateSection,
  type MemoryListParams,
} from '../../lib/api/memories'
import type {
  AgentSettings,
  MemoryCategory,
  MemoryFileRecord,
  MemoryRecord,
  MemoryReviewState,
  MemorySectionRecord,
  MemoryStatus,
} from '../../lib/api/types'
import { MemoryRetrievalDebugger } from '../../components/MemoryRetrievalDebugger'
import { Dropdown, type DropdownOption } from '../../components/Dropdown'

const MEMORY_CATEGORIES: MemoryCategory[] = ['preference', 'behavior', 'fact', 'project', 'workflow', 'reflection']
const MIN_MANUAL_MEMORY_CHARS = 8

const WRITE_POLICY_OPTIONS: DropdownOption<AgentSettings['memory']['write_policy']>[] = [
  { value: 'auto_with_review', label: 'Auto with review' },
  { value: 'auto_reviewed', label: 'Auto reviewed' },
  { value: 'manual', label: 'Manual only' },
  { value: 'off', label: 'Off' },
]

const EMPTY_STATS = {
  total: 0,
  active: 0,
  archived: 0,
  new: 0,
  reviewed: 0,
  fact: 0,
  reflection: 0,
  candidates: 0,
  unresolved_candidates: 0,
  short_term: 0,
  personalities: 0,
  curated_sessions: 0,
  audit_events: 0,
  archived_messages: 0,
  memory_root: '',
  categories: {} as Partial<Record<MemoryCategory, number>>,
}

interface Props {
  draft: AgentSettings
  updateDraft: (updater: (current: AgentSettings) => AgentSettings) => void
}

export function MemorySettingsPanel({ draft, updateDraft }: Props) {
  const [query, setQuery] = useState('')
  const [activeTab, setActiveTab] = useState<MemoryCategory>('preference')
  const [statusFilter, setStatusFilter] = useState<'' | MemoryStatus>('active')
  const [reviewFilter, setReviewFilter] = useState<'' | MemoryReviewState>('')
  const [memories, setMemories] = useState<MemoryRecord[]>([])
  const [memoryFile, setMemoryFile] = useState<MemoryFileRecord | null>(null)
  const [rawMarkdown, setRawMarkdown] = useState('')
  const [stats, setStats] = useState(EMPTY_STATS)
  const [loading, setLoading] = useState(false)
  const [editWholeFile, setEditWholeFile] = useState(false)
  const [newContent, setNewContent] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState('')
  const [editingBody, setEditingBody] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [mutationError, setMutationError] = useState('')
  const requestSeq = useRef(0)
  const trimmedNewContent = newContent.trim()
  const canAddMemory = trimmedNewContent.length >= MIN_MANUAL_MEMORY_CHARS

  const activeFilter = (() => {
    if (statusFilter === 'archived') return 'archived'
    if (reviewFilter === 'new') return 'needs_review'
    if (statusFilter === 'active') return 'active'
    return ''
  })()

  const applyQuickFilter = (key: string) => {
    if (key === activeFilter) return
    switch (key) {
      case 'active': setStatusFilter('active'); setReviewFilter(''); break
      case 'needs_review': setStatusFilter('active'); setReviewFilter('new'); break
      case 'archived': setStatusFilter('archived'); setReviewFilter(''); break
    }
  }

  const listParams = useMemo<MemoryListParams>(() => ({
    query,
    category: activeTab,
    status: statusFilter,
    review_state: reviewFilter,
    limit: 100,
  }), [activeTab, query, reviewFilter, statusFilter])

  const loadMemoryData = useCallback(async ({
    silent = false,
    clearOnError = false,
  }: {
    silent?: boolean
    clearOnError?: boolean
  } = {}) => {
    const requestId = ++requestSeq.current
    if (!silent) setLoading(true)
    try {
      const memoryFilePromise = typeof fetchMemoryFile === 'function'
        ? fetchMemoryFile(activeTab).catch(() => null)
        : Promise.resolve<MemoryFileRecord | null>(null)
      const [items, file, nextStats] = await Promise.all([
        fetchMemories(listParams),
        memoryFilePromise,
        fetchMemoryStats(),
      ])
      if (requestId !== requestSeq.current) return
      setMemories(items)
      setMemoryFile(file)
      setRawMarkdown(file?.raw_markdown ?? '')
      setStats(nextStats)
      setLoadError('')
    } catch {
      if (requestId !== requestSeq.current) return
      setLoadError('Memory data could not be refreshed.')
      if (clearOnError) {
        setMemories([])
        setMemoryFile(null)
        setStats(EMPTY_STATS)
      }
    } finally {
      if (requestId === requestSeq.current && !silent) setLoading(false)
    }
  }, [activeTab, listParams])

  useEffect(() => {
    loadMemoryData({ clearOnError: true })
    const refresh = () => loadMemoryData({ silent: true })
    const intervalId = window.setInterval(refresh, 10000)
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      requestSeq.current += 1
      window.clearInterval(intervalId)
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [loadMemoryData])

  const sections = useMemo(() => {
    if (statusFilter === 'active' && !query && !reviewFilter && memoryFile) {
      return memoryFile.sections
    }
    const activeById = new Map((memoryFile?.sections ?? []).map((section) => [section.id, section]))
    return memories.map((memory) => activeById.get(memory.id) ?? recordToSection(memory))
  }, [memories, memoryFile, query, reviewFilter, statusFilter])

  const updateMemorySettings = (patch: Partial<AgentSettings['memory']>) => {
    updateDraft((current) => ({
      ...current,
      memory: { ...current.memory, ...patch },
    }))
  }

  const updateNumberSetting = (
    key: 'retrieval_limit' | 'max_injected_chars' | 'maintenance_cooldown_hours',
    value: string,
    min: number,
    max: number,
  ) => {
    const parsed = Number(value)
    const next = Number.isFinite(parsed) ? Math.min(max, Math.max(min, Math.trunc(parsed))) : min
    updateMemorySettings({ [key]: next })
  }

  const addMemory = async () => {
    const content = trimmedNewContent
    setMutationError('')
    if (!content) return
    if (content.length < MIN_MANUAL_MEMORY_CHARS) {
      setMutationError(`Memory must be at least ${MIN_MANUAL_MEMORY_CHARS} characters.`)
      return
    }
    try {
      await createMemory({
        content,
        category: activeTab,
        review_state: 'reviewed',
        confidence: 1,
        importance: 5,
        kind: 'fact',
      })
      setNewContent('')
      await loadMemoryData()
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : 'Memory could not be saved.')
    }
  }

  const startEditing = (section: MemorySectionRecord) => {
    setExpandedId(section.id)
    setEditingTitle(section.title)
    setEditingBody(section.body)
  }

  const saveSection = async (section: MemorySectionRecord) => {
    try {
      setMutationError('')
      await updateSection(section.id, {
        title: editingTitle,
        body: editingBody,
        review_state: section.review_state === 'new' ? 'reviewed' : section.review_state,
      })
      setExpandedId(null)
      await loadMemoryData()
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : 'Memory section could not be saved.')
    }
  }

  const archiveSection = async (section: MemorySectionRecord) => {
    try {
      setMutationError('')
      await updateMemory(section.id, { status: 'archived' })
      setExpandedId(null)
      await loadMemoryData()
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : 'Memory section could not be archived.')
    }
  }

  const removeSection = async (section: MemorySectionRecord) => {
    try {
      setMutationError('')
      await deleteMemory(section.id)
      setExpandedId(null)
      await loadMemoryData()
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : 'Memory section could not be deleted.')
    }
  }

  const markReviewed = async (section: MemorySectionRecord) => {
    try {
      setMutationError('')
      await updateSection(section.id, { review_state: 'reviewed' })
      await loadMemoryData()
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : 'Memory section could not be updated.')
    }
  }

  const saveWholeFile = async () => {
    try {
      setMutationError('')
      const saved = await saveMemoryFile(activeTab, rawMarkdown)
      setMemoryFile(saved)
      setRawMarkdown(saved.raw_markdown)
      await loadMemoryData()
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : 'Memory file could not be saved.')
    }
  }

  return (
    <div className="max-w-5xl space-y-3">
      {loadError && <Alert>{loadError}</Alert>}
      {mutationError && <Alert>{mutationError}</Alert>}

      {/* Category tabs */}
      <div className="flex flex-wrap items-center gap-1 rounded-xl border border-white/[0.07] bg-black/10 p-1">
        {MEMORY_CATEGORIES.map((category) => (
          <button
            key={category}
            type="button"
            onClick={() => {
              setActiveTab(category)
              setExpandedId(null)
              setEditWholeFile(false)
            }}
            className={`inline-flex h-8 items-center gap-2 rounded-lg px-3 text-[11px] font-medium transition-colors ${
              activeTab === category
                ? 'bg-white/[0.08] text-neutral-100'
                : 'text-neutral-500 hover:bg-white/[0.04] hover:text-neutral-300'
            }`}
          >
            {formatLabel(category)}
            <span className="rounded bg-black/20 px-1.5 py-0.5 text-[10px] tabular-nums text-neutral-400">
              {stats.categories?.[category] ?? 0}
            </span>
          </button>
        ))}
      </div>

      {/* Toolbar: filter + search + actions */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-0.5 rounded-lg border border-white/[0.07] bg-black/10 p-0.5">
          {([
            { key: 'active', label: 'Active' },
            { key: 'needs_review', label: 'Review' },
            { key: 'archived', label: 'Archived' },
          ] as const).map(({ key, label }) => (
            <button
              key={key}
              type="button"
              onClick={() => applyQuickFilter(key)}
              className={`h-6 rounded-md px-2 text-[10px] transition-colors ${
                activeFilter === key
                  ? 'bg-white/[0.08] font-medium text-neutral-200'
                  : 'text-neutral-500 hover:text-neutral-300'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="relative min-w-[140px] flex-1">
          <Search size={12} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-neutral-600" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search..."
            className="control h-7 w-full rounded-lg pl-7 pr-2.5 text-[11px]"
          />
        </div>

        <button type="button" onClick={() => loadMemoryData()} disabled={loading} className="ghost-button h-7 w-7 rounded-lg" aria-label="Refresh">
          <RotateCcw size={12} className={loading ? 'animate-spin' : ''} />
        </button>
        <button
          type="button"
          onClick={() => setEditWholeFile((v) => !v)}
          className={`ghost-button h-7 w-7 rounded-lg ${editWholeFile ? 'text-accent-light' : ''}`}
          aria-label="Edit whole file"
          title="Edit whole file"
        >
          <FileText size={12} />
        </button>
        <button
          type="button"
          onClick={() => setShowSettings((v) => !v)}
          className={`ghost-button h-7 w-7 rounded-lg ${showSettings ? 'text-accent-light' : ''}`}
          aria-label="Memory settings"
        >
          <Settings2 size={12} />
        </button>
      </div>

      <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
        <MemoryStat label="Active" value={stats.active} />
        <MemoryStat label="Review" value={stats.new} />
        <MemoryStat label="Archived" value={stats.archived} />
        <MemoryStat label="Summaries" value={stats.short_term} />
        <MemoryStat label="Profiles" value={stats.personalities} />
        <MemoryStat label="Curated" value={stats.curated_sessions} />
      </div>

      {/* Add memory */}
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={newContent}
          onChange={(event) => {
            setNewContent(event.target.value)
            setMutationError('')
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && canAddMemory) {
              event.preventDefault()
              addMemory()
            }
          }}
          placeholder="Add a memory…"
          className="control h-7 min-w-[220px] flex-1 rounded-lg px-2.5 text-[11px]"
        />
        <button
          type="button"
          onClick={addMemory}
          disabled={!canAddMemory}
          aria-label="Add memory"
          className="primary-button h-7 rounded-lg px-3 text-[11px] font-medium disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Plus size={12} />
          Add
        </button>
      </div>
      {trimmedNewContent && !canAddMemory && (
        <p className="text-[10px] text-amber-200">
          Use at least {MIN_MANUAL_MEMORY_CHARS} characters.
        </p>
      )}

      {/* Content */}
      {editWholeFile ? (
        <div className="rounded-xl border border-white/[0.07] bg-white/[0.025] p-3">
          <textarea
            value={rawMarkdown}
            onChange={(event) => setRawMarkdown(event.target.value)}
            rows={18}
            className="control min-h-[420px] w-full resize-y rounded-lg px-3 py-2 font-mono text-xs leading-relaxed"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button type="button" onClick={() => setRawMarkdown(memoryFile?.raw_markdown ?? '')} className="ghost-button h-7 rounded-md px-2 text-[11px]">
              Cancel
            </button>
            <button type="button" onClick={saveWholeFile} className="primary-button h-7 rounded-md px-3 text-[11px]">
              <Save size={13} />
              Save
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-1.5">
          {loading && (
            <div className="rounded-xl border border-white/[0.07] bg-white/[0.025] px-3 py-2.5 text-xs text-neutral-500">
              Loading...
            </div>
          )}
          {!loading && sections.length === 0 && (
            <div className="rounded-xl border border-white/[0.07] bg-white/[0.025] px-3 py-2.5 text-xs text-neutral-500">
              No sections found.
            </div>
          )}
          {sections.map((section) => {
            const expanded = expandedId === section.id
            return (
              <div key={section.id} className="rounded-xl border border-white/[0.07] bg-white/[0.025]">
                <button
                  type="button"
                  onClick={() => (expanded ? setExpandedId(null) : startEditing(section))}
                  className="block w-full px-3 py-2.5 text-left"
                >
                  <div className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-xs font-medium text-neutral-200">
                      {section.title}
                    </span>
                    <span className="shrink-0 rounded bg-white/[0.05] px-1.5 py-0.5 text-[10px] tabular-nums text-neutral-500">
                      I{section.importance}
                    </span>
                    <span
                      className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                        section.review_state === 'new' ? 'bg-amber-400' : 'bg-emerald-400'
                      }`}
                      title={formatLabel(section.review_state)}
                    />
                  </div>
                  {!expanded && (
                    <p className="mt-1 line-clamp-1 text-[11px] leading-relaxed text-neutral-500">
                      {sectionPreview(section.body || section.content)}
                    </p>
                  )}
                </button>

                {expanded && (
                  <div className="border-t border-white/[0.06] px-3 py-3">
                    <input
                      value={editingTitle}
                      onChange={(event) => setEditingTitle(event.target.value)}
                      className="control mb-2 h-8 w-full rounded-lg px-2.5 text-xs font-semibold"
                    />
                    <textarea
                      value={editingBody}
                      onChange={(event) => setEditingBody(event.target.value)}
                      rows={8}
                      className="control w-full resize-y rounded-lg px-3 py-2 font-mono text-xs leading-relaxed"
                    />
                    <div className="mt-2 flex justify-end gap-1.5">
                      {section.review_state === 'new' && (
                        <button type="button" onClick={() => markReviewed(section)} className="ghost-button h-7 w-7 rounded-md" aria-label="Mark reviewed">
                          <CheckCircle2 size={13} />
                        </button>
                      )}
                      {section.status === 'active' && (
                        <button type="button" onClick={() => archiveSection(section)} className="ghost-button h-7 w-7 rounded-md" aria-label="Archive">
                          <Archive size={13} />
                        </button>
                      )}
                      <button type="button" onClick={() => removeSection(section)} className="ghost-button h-7 w-7 rounded-md hover:text-red-300" aria-label="Delete">
                        <Trash2 size={13} />
                      </button>
                      <div className="mx-1 h-5 w-px bg-white/[0.07]" />
                      <button type="button" onClick={() => setExpandedId(null)} className="ghost-button h-7 rounded-md px-2 text-[11px]">
                        Cancel
                      </button>
                      <button type="button" onClick={() => saveSection(section)} className="primary-button h-7 rounded-md px-3 text-[11px]">
                        <Save size={13} />
                        Save
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Settings — collapsed, toggled by gear icon */}
      {showSettings && (
        <div className="panel-muted space-y-2 rounded-xl px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <ToggleChip label="Memory" checked={draft.memory.enabled} onChange={(enabled) => updateMemorySettings({ enabled })} />
            <ToggleChip label="Auto learn" checked={draft.memory.auto_learn} onChange={(auto_learn) => updateMemorySettings({ auto_learn })} />
            <ToggleChip label="Session curate" checked={draft.memory.curate_on_session_close} onChange={(curate_on_session_close) => updateMemorySettings({ curate_on_session_close })} />
            <div className="h-5 w-px bg-white/[0.07]" />
            <SettingField label="Write policy">
              <Dropdown value={draft.memory.write_policy} options={WRITE_POLICY_OPTIONS} onChange={(write_policy) => updateMemorySettings({ write_policy })} ariaLabel="Write policy" size="sm" className="w-36" />
            </SettingField>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-white/[0.06] pt-2">
            <SettingField label="Retrieved">
              <NumberField value={draft.memory.retrieval_limit} min={1} max={20} onChange={(value) => updateNumberSetting('retrieval_limit', value, 1, 20)} ariaLabel="Retrieved memories" />
            </SettingField>
            <SettingField label="Budget">
              <NumberField value={draft.memory.max_injected_chars} min={500} max={10000} step={100} onChange={(value) => updateNumberSetting('max_injected_chars', value, 500, 10000)} ariaLabel="Prompt budget" />
            </SettingField>
            <SettingField label="Min confidence">
              <NumberField value={draft.memory.min_confidence} min={0} max={1} step={0.05} onChange={(value) => updateMemorySettings({ min_confidence: Math.min(1, Math.max(0, Number(value) || 0)) })} ariaLabel="Minimum confidence" />
            </SettingField>
            <SettingField label="Min relevance">
              <NumberField value={draft.memory.min_relevance_score} min={0} max={1} step={0.05} onChange={(value) => updateMemorySettings({ min_relevance_score: Math.min(1, Math.max(0, Number(value) || 0)) })} ariaLabel="Minimum relevance" />
            </SettingField>
            <SettingField label="Maintenance">
              <NumberField value={draft.memory.maintenance_cooldown_hours} min={0} max={168} onChange={(value) => updateNumberSetting('maintenance_cooldown_hours', value, 0, 168)} ariaLabel="Maintenance cooldown hours" />
            </SettingField>
          </div>
          {stats.memory_root && (
            <div className="truncate text-[10px] text-neutral-500" title={stats.memory_root}>
              Folder: {stats.memory_root}
            </div>
          )}
          <MemoryRetrievalDebugger initialQuery={query} />
        </div>
      )}
    </div>
  )
}

function Alert({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-amber-400/20 bg-amber-400/10 px-3 py-2 text-xs text-amber-200">
      {children}
    </div>
  )
}

function MemoryStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-white/[0.07] bg-white/[0.025] px-2.5 py-2">
      <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-neutral-500">{label}</p>
      <p className="mt-1 text-sm font-semibold tabular-nums text-neutral-100">{value}</p>
    </div>
  )
}

function recordToSection(memory: MemoryRecord): MemorySectionRecord {
  return {
    ...memory,
    title: sectionPreview(memory.content).slice(0, 80) || memory.id,
    body: memory.content,
  }
}

function sectionPreview(value: string) {
  return String(value || '').replace(/^[-*]\s+/gm, '').replace(/\s+/g, ' ').trim()
}

function ToggleChip({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="inline-flex h-7 cursor-pointer items-center gap-2 rounded-lg border border-white/[0.08] bg-white/[0.03] px-2.5 text-[11px] text-neutral-300 transition-colors hover:text-neutral-100">
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} className="h-3 w-3 accent-[#8bcf4f]" />
      <span>{label}</span>
    </label>
  )
}

function SettingField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-neutral-500">{label}</span>
      {children}
    </div>
  )
}

function NumberField({
  value,
  min,
  max,
  step,
  onChange,
  ariaLabel,
  className = '',
}: {
  value: number
  min: number
  max: number
  step?: number
  onChange: (value: string) => void
  ariaLabel: string
  className?: string
}) {
  return (
    <input
      type="number"
      value={value}
      min={min}
      max={max}
      step={step}
      onChange={(event) => onChange(event.target.value)}
      aria-label={ariaLabel}
      className={`control runtime-limit-input h-7 rounded-lg px-2 text-[11px] tabular-nums ${className || 'w-20'}`}
    />
  )
}

function formatLabel(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}
