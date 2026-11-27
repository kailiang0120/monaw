import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Clock3,
  Play,
  RefreshCw,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { MessageDialog } from '../../components/ui/MessageDialog'
import { Badge, Note, SettingsCard } from '../../components/ui/Panel'
import { Dropdown } from '../../components/Dropdown'
import { SupportReport } from './SupportReport'
import {
  deleteRuntimeData,
  enableSupportMode,
  exportObservabilityDebugBundle,
  fetchBackendLogTail,
  fetchObservabilityErrors,
  fetchObservabilityRun,
  fetchObservabilityRuns,
  fetchObservabilitySummary,
  replayObservabilityRun,
} from '../../lib/api/diagnostics'
import { ApiError } from '../../lib/api/client'
import type {
  ObservabilityBackendLog,
  ObservabilityError,
  ObservabilityRun,
  ObservabilityRunDetail,
  ObservabilitySummary,
} from '../../lib/api/types'
import { formatBytes } from '../../lib/formatBytes'

type ViewMode = 'runs' | 'errors' | 'backend' | 'report'
const VIRTUALIZE_OBSERVABILITY_THRESHOLD = 60
const RUN_ROW_HEIGHT_PX = 62
const ERROR_ROW_HEIGHT_PX = 56
const VIRTUAL_OVERSCAN = 8

const EMPTY_SUMMARY: ObservabilitySummary = {
  total_runs: 0,
  successful_runs: 0,
  success_rate: 0,
  failed_runs: 0,
  total_tokens: 0,
  estimated_cost_usd: 0,
  average_duration_ms: 0,
  tool_error_count: 0,
  storage_path: '',
  token_usage_over_time: [],
  duration_trend: [],
  top_error_reasons: [],
  top_failing_tools: [],
  model_usage: [],
  storage_metrics: {},
  runtime_metrics: {},
  field_classification: {},
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat().format(Math.round(value || 0))
}

function formatCost(value: number): string {
  if (!value) return '$0.00'
  return `$${value.toFixed(value < 0.01 ? 5 : 2)}`
}

function metricNumber(metrics: Record<string, unknown>, key: string): number {
  const value = metrics[key]
  return typeof value === 'number' ? value : 0
}

function formatDuration(value: number): string {
  if (!value) return '-'
  if (value < 1000) return `${Math.round(value)}ms`
  const seconds = value / 1000
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
}

function formatTime(value: string): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function shortId(value: string): string {
  return value ? value.replace(/^run_/, '').slice(0, 8) : '-'
}

function statusTone(status: string): 'ok' | 'accent' | 'warn' | 'danger' {
  const normalized = status.toLowerCase()
  if (normalized === 'complete') return 'ok'
  if (normalized === 'running') return 'accent'
  if (normalized === 'paused') return 'warn'
  return 'danger'
}

function statusDotClass(status: string): string {
  const tone = statusTone(status)
  if (tone === 'ok') return 'st-dot st-dot-ok'
  if (tone === 'warn') return 'st-dot st-dot-warn'
  if (tone === 'danger') return 'st-dot st-dot-danger'
  return 'st-dot'
}

function jsonText(value: unknown): string {
  if (value == null || value === '') return '-'
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

export function ObservabilityPanel({ refreshKey = 0 }: { refreshKey?: number }) {
  const [summary, setSummary] = useState<ObservabilitySummary>(EMPTY_SUMMARY)
  const [runs, setRuns] = useState<ObservabilityRun[]>([])
  const [errors, setErrors] = useState<ObservabilityError[]>([])
  const [backendLog, setBackendLog] = useState<ObservabilityBackendLog | null>(null)
  const [supportModeRequired, setSupportModeRequired] = useState(false)
  const [enablingSupportMode, setEnablingSupportMode] = useState(false)
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedRun, setSelectedRun] = useState<ObservabilityRunDetail | null>(null)
  const [view, setView] = useState<ViewMode>('runs')
  const [statusFilter, setStatusFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [actionRunId, setActionRunId] = useState('')
  const [deletingData, setDeletingData] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const refreshAbortRef = useRef<AbortController | null>(null)
  const detailAbortRef = useRef<AbortController | null>(null)

  const loadSummary = async (signal?: AbortSignal) => {
    setSummary(await fetchObservabilitySummary(signal))
  }

  const loadRuns = async (signal?: AbortSignal) => {
    setRuns(await fetchObservabilityRuns({
      status: statusFilter,
      source: sourceFilter,
      q: query,
      limit: 100,
      signal,
    }))
  }

  const loadErrors = async (signal?: AbortSignal) => {
    setErrors(await fetchObservabilityErrors({ q: query, limit: 100, signal }))
  }

  /**
   * Reading backend.log needs support mode, so a 403 is a normal state, not a
   * failure — it means "ask the user to turn support mode on", and must not
   * surface as a red banner over the rest of the panel.
   */
  const loadBackendLog = async (signal?: AbortSignal) => {
    try {
      setBackendLog(await fetchBackendLogTail(500, signal))
      setSupportModeRequired(false)
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 403) {
        setBackendLog(null)
        setSupportModeRequired(true)
        return
      }
      throw exc
    }
  }

  const refreshAll = async () => {
    refreshAbortRef.current?.abort()
    const controller = new AbortController()
    refreshAbortRef.current = controller
    setLoading(true)
    setError('')
    try {
      await Promise.all([
        loadSummary(controller.signal),
        loadRuns(controller.signal),
        loadErrors(controller.signal),
      ])
      if (selectedRunId) {
        setSelectedRun(await fetchObservabilityRun(selectedRunId, controller.signal))
      }
    } catch (exc) {
      if (controller.signal.aborted) return
      setError(exc instanceof Error ? exc.message : 'Failed to load observability data')
    } finally {
      if (refreshAbortRef.current === controller) {
        refreshAbortRef.current = null
        setLoading(false)
      }
    }
  }

  useEffect(() => {
    void refreshAll()
    return () => {
      refreshAbortRef.current?.abort()
      detailAbortRef.current?.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!refreshKey) return
    void refreshAll()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  useEffect(() => {
    const controller = new AbortController()
    void (async () => {
      try {
        if (view === 'runs') {
          await loadRuns(controller.signal)
          return
        }
        if (view === 'errors') {
          await loadErrors(controller.signal)
        }
      } catch (exc) {
        if (controller.signal.aborted) return
        setError(exc instanceof Error ? exc.message : 'Failed to apply filters')
      }
    })()
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, sourceFilter, query, view])

  useEffect(() => {
    if (view !== 'backend') return
    const controller = new AbortController()
    void (async () => {
      try {
        await loadBackendLog(controller.signal)
      } catch (exc) {
        if (controller.signal.aborted) return
        setError(exc instanceof Error ? exc.message : 'Failed to load backend log')
      }
    })()
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view])

  const turnOnSupportMode = async () => {
    setEnablingSupportMode(true)
    setError('')
    try {
      await enableSupportMode(600)
      await loadBackendLog()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Failed to enable support mode')
    } finally {
      setEnablingSupportMode(false)
    }
  }

  const selectRun = async (runId: string) => {
    setSelectedRunId(runId)
    setDetailLoading(true)
    setError('')
    detailAbortRef.current?.abort()
    const controller = new AbortController()
    detailAbortRef.current = controller
    try {
      setSelectedRun(await fetchObservabilityRun(runId, controller.signal))
    } catch (exc) {
      if (controller.signal.aborted) return
      setError(exc instanceof Error ? exc.message : 'Failed to load run detail')
    } finally {
      if (detailAbortRef.current === controller) {
        detailAbortRef.current = null
        setDetailLoading(false)
      }
    }
  }

  const closeRunDetail = () => {
    detailAbortRef.current?.abort()
    detailAbortRef.current = null
    setDetailLoading(false)
    setSelectedRunId('')
    setSelectedRun(null)
  }

  const replayRun = async (runId: string) => {
    setActionRunId(runId)
    setNotice('')
    setError('')
    try {
      const result = await replayObservabilityRun(runId)
      setNotice(`Replay ${shortId(result.replay_run_id)} finished as ${result.status}.`)
      await refreshAll()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Replay failed')
    } finally {
      setActionRunId('')
    }
  }

  const exportBundle = async (runId: string) => {
    setActionRunId(runId)
    setNotice('')
    setError('')
    try {
      const result = await exportObservabilityDebugBundle(runId)
      setNotice(`Debug bundle exported: ${result.filename}${result.encrypted ? ' (encrypted)' : ''}.`)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Export failed')
    } finally {
      setActionRunId('')
    }
  }

  const deleteData = async () => {
    setDeletingData(true)
    setNotice('')
    setError('')
    try {
      const result = await deleteRuntimeData()
      const deletedCount = Object.values(result.deleted).reduce((total, value) => total + Number(value || 0), 0)
      setSelectedRunId('')
      setSelectedRun(null)
      setNotice(`Runtime data deleted across ${formatNumber(deletedCount)} stored item${deletedCount === 1 ? '' : 's'}.`)
      await refreshAll()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Deletion failed')
    } finally {
      setDeletingData(false)
    }
  }

  const trendMax = useMemo(() => {
    return Math.max(1, ...summary.token_usage_over_time.map((item) => Number(item.tokens || 0)))
  }, [summary.token_usage_over_time])

  const durationTrendMax = useMemo(() => {
    return Math.max(1, ...summary.duration_trend.map((item) => Number(item.average_duration_ms || 0)))
  }, [summary.duration_trend])

  const storageBytes = metricNumber(summary.storage_metrics, 'total_bytes')
  const droppedEvents = metricNumber(summary.runtime_metrics, 'dropped_events')
  const successRatePercent = Math.round((summary.success_rate || 0) * 100)

  const viewCountLabel = view === 'runs'
    ? `${formatNumber(runs.length)} run${runs.length === 1 ? '' : 's'}`
    : view === 'errors'
      ? `${formatNumber(errors.length)} error${errors.length === 1 ? '' : 's'}`
      : null

  return (
    <div className="space-y-4">
      {confirmDelete && (
        <MessageDialog
          title="Delete runtime data?"
          message="Delete runtime diagnostics, observability data, support artifacts, memory files, approvals, and scheduled-task output?"
          confirmLabel="Delete data"
          cancelLabel="Cancel"
          destructive
          onClose={(confirmed) => {
            setConfirmDelete(false)
            if (confirmed) void deleteData()
          }}
        />
      )}

      <SettingsCard
        title="Overview"
        action={
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void refreshAll()}
              disabled={loading || deletingData}
              className="st-btn st-btn-ghost st-btn-sm"
            >
              <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
              Refresh
            </button>
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              disabled={loading || deletingData}
              className="st-btn st-btn-danger st-btn-sm"
            >
              <Trash2 size={13} />
              Delete Data
            </button>
          </div>
        }
      >
        <div className="st-obs-stat-grid">
          <div className="st-obs-stat">
            <p className="st-hint">Total runs</p>
            <p className="mt-1 text-lg font-semibold" style={{ color: 'var(--st-text)' }}>
              {formatNumber(summary.total_runs)}
            </p>
            <p className="mt-0.5 text-[11px]" style={{ color: 'var(--st-text-muted)' }}>
              {successRatePercent}% success
              {summary.failed_runs > 0 && (
                <span style={{ color: 'var(--st-warn)' }}> · {formatNumber(summary.failed_runs)} failed</span>
              )}
            </p>
          </div>

          <div className="st-obs-stat">
            <p className="st-hint">Total tokens</p>
            <p className="mt-1 text-lg font-semibold" style={{ color: 'var(--st-text)' }}>
              {formatNumber(summary.total_tokens)}
            </p>
            <p className="mt-0.5 text-[11px]" style={{ color: 'var(--st-text-muted)' }}>
              {formatCost(summary.estimated_cost_usd)} est.
            </p>
          </div>

          <div className="st-obs-stat">
            <p className="st-hint">Avg duration</p>
            <p className="mt-1 text-lg font-semibold" style={{ color: 'var(--st-text)' }}>
              {formatDuration(summary.average_duration_ms)}
            </p>
            <p className="mt-0.5 text-[11px]" style={{ color: 'var(--st-text-muted)' }}>
              per run
            </p>
          </div>

          <div className="st-obs-stat">
            <p className="st-hint">Tool errors</p>
            <p
              className="mt-1 text-lg font-semibold"
              style={{ color: summary.tool_error_count > 0 ? 'var(--st-warn)' : 'var(--st-text)' }}
            >
              {formatNumber(summary.tool_error_count)}
            </p>
            <p
              className="mt-0.5 text-[11px]"
              style={{ color: droppedEvents > 0 ? 'var(--st-warn)' : 'var(--st-text-muted)' }}
            >
              {droppedEvents > 0 ? `${formatNumber(droppedEvents)} dropped` : 'across runs'}
            </p>
          </div>
        </div>

        <details className="st-obs-disclosure st-divider-t">
          <summary className="flex items-center justify-between gap-3 px-4 py-2.5 text-xs font-medium transition-colors hover:bg-[var(--st-surface-hover)]">
            <span style={{ color: 'var(--st-text)' }}>Trends &amp; breakdown</span>
            <span className="flex items-center gap-2 st-hint">
              <span>{formatBytes(storageBytes)} stored</span>
              <ChevronDown size={14} className="st-obs-disclosure-chevron" aria-hidden="true" />
            </span>
          </summary>

          <div className="st-divider-t space-y-4 p-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <MiniChart
                title="Token usage"
                items={summary.token_usage_over_time.map((item) => ({
                  label: item.day.slice(5),
                  value: Number(item.tokens || 0),
                  caption: `${formatNumber(item.runs)} runs`,
                }))}
                max={trendMax}
              />
              <MiniChart
                title="Duration"
                items={summary.duration_trend.map((item) => ({
                  label: item.day.slice(5),
                  value: Number(item.average_duration_ms || 0),
                  caption: formatDuration(Number(item.average_duration_ms || 0)),
                }))}
                max={durationTrendMax}
              />
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <RankList
                  title="Model usage"
                  items={summary.model_usage.map((item) => ({
                    label: `${item.provider}/${item.model}`,
                    value: item.runs,
                    caption: `${formatNumber(item.tokens)} tokens`,
                  }))}
                />
              </div>
              <RankList
                title="Top error reasons"
                items={summary.top_error_reasons.map((item) => ({ label: item.reason, value: item.count }))}
              />
              <RankList
                title="Failing tools"
                items={summary.top_failing_tools.map((item) => ({ label: item.tool_name || 'unknown', value: item.count }))}
              />
            </div>

            <div className="st-divider-t flex flex-wrap items-center justify-between gap-2 pt-3 text-[11px]" style={{ color: 'var(--st-text-faint)' }}>
              <span className="min-w-0 truncate" title={summary.storage_path || undefined}>
                {summary.storage_path || 'Storage path unavailable'}
              </span>
              <span className="shrink-0">
                {formatNumber(summary.top_error_reasons.length)} error pattern{summary.top_error_reasons.length === 1 ? '' : 's'}
                {' · '}
                {formatNumber(droppedEvents)} dropped event{droppedEvents === 1 ? '' : 's'}
              </span>
            </div>
          </div>
        </details>
      </SettingsCard>

      {error && (
        <div role="alert">
          <Note tone="danger">{error}</Note>
        </div>
      )}
      {notice && <Note tone="info">{notice}</Note>}

      <div className="space-y-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="st-segment" role="group" aria-label="Activity views">
            {(['runs', 'errors', 'backend', 'report'] as ViewMode[]).map((item) => (
              <button
                key={item}
                type="button"
                aria-pressed={view === item}
                onClick={() => setView(item)}
                className="st-segment-item"
              >
                {item === 'runs' ? 'Runs' : item === 'errors' ? 'Errors' : item === 'backend' ? 'Backend log' : 'Support report'}
              </button>
            ))}
          </div>
          {viewCountLabel && (
            <span className="st-hint">{viewCountLabel}</span>
          )}
        </div>

        {(view === 'runs' || view === 'errors') && (
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[180px] flex-1">
              <Search
                size={13}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2"
                style={{ color: 'var(--st-text-faint)' }}
              />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search traces"
                aria-label="Search traces"
                className="st-input st-search"
              />
            </div>
            {view === 'runs' && (
              <>
                <Dropdown
                  ariaLabel="Filter by status"
                  className="w-32"
                  size="sm"
                  value={statusFilter}
                  onChange={setStatusFilter}
                  options={[
                    { value: '', label: 'All status' },
                    { value: 'complete', label: 'Complete' },
                    { value: 'paused', label: 'Paused' },
                    { value: 'error', label: 'Error' },
                    { value: 'running', label: 'Running' },
                  ]}
                />
                <Dropdown
                  ariaLabel="Filter by source"
                  className="w-32"
                  size="sm"
                  value={sourceFilter}
                  onChange={setSourceFilter}
                  options={[
                    { value: '', label: 'All sources' },
                    { value: 'desktop', label: 'Desktop' },
                    { value: 'telegram', label: 'Telegram' },
                    { value: 'scheduled', label: 'Scheduled' },
                    { value: 'replay', label: 'Replay' },
                  ]}
                />
              </>
            )}
          </div>
        )}
      </div>

      {view === 'runs' && (
        <div className="space-y-3">
          <RunsTable
            runs={runs}
            selectedRunId={selectedRunId}
            loading={loading}
            onSelect={(runId) => void selectRun(runId)}
          />
          {(selectedRunId || detailLoading) && (
            <RunDetail
              run={selectedRun}
              loading={detailLoading}
              actionRunId={actionRunId}
              onReplay={(runId) => void replayRun(runId)}
              onExport={(runId) => void exportBundle(runId)}
              onClose={closeRunDetail}
            />
          )}
        </div>
      )}

      {view === 'errors' && <ErrorsList errors={errors} loading={loading} />}
      {view === 'report' && <SupportReport />}
      {view === 'backend' && (
        <BackendLogView
          log={backendLog}
          loading={loading}
          supportModeRequired={supportModeRequired}
          enabling={enablingSupportMode}
          onEnableSupportMode={turnOnSupportMode}
        />
      )}
    </div>
  )
}

function MiniChart({
  title,
  items,
  max,
}: {
  title: string
  items: Array<{ label: string; value: number; caption?: string }>
  max: number
}) {
  return (
    <div
      className="rounded-lg border p-3"
      style={{ borderColor: 'var(--st-border)', background: 'var(--st-surface-sunken)' }}
    >
      <p className="st-hint mb-2.5 font-medium">{title}</p>
      {items.length === 0 ? (
        <p className="py-5 text-center text-xs" style={{ color: 'var(--st-text-faint)' }}>No data yet.</p>
      ) : (
        <div className="flex h-24 items-end gap-1.5">
          {items.map((item) => (
            <div key={item.label} className="flex min-w-0 flex-1 flex-col items-center gap-1">
              <div
                className="flex h-16 w-full items-end overflow-hidden rounded"
                style={{ background: 'var(--st-surface-muted)' }}
              >
                <div
                  className="w-full rounded"
                  style={{
                    height: `${Math.max(6, (item.value / max) * 100)}%`,
                    background: 'var(--st-accent)',
                  }}
                  title={`${item.label}: ${formatNumber(item.value)} ${item.caption || ''}`}
                />
              </div>
              <span className="truncate text-[10px]" style={{ color: 'var(--st-text-faint)' }}>{item.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function RankList({
  title,
  items,
}: {
  title: string
  items: Array<{ label: string; value: number; caption?: string }>
}) {
  const max = Math.max(1, ...items.map((item) => item.value))
  return (
    <div
      className="rounded-lg border p-3"
      style={{ borderColor: 'var(--st-border)', background: 'var(--st-surface-sunken)' }}
    >
      <p className="st-hint mb-2.5 font-medium">{title}</p>
      {items.length === 0 ? (
        <p className="py-4 text-center text-xs" style={{ color: 'var(--st-text-faint)' }}>No data yet.</p>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <div key={item.label} className="space-y-1">
              <div className="flex items-center justify-between gap-2 text-xs">
                <span className="min-w-0 truncate" style={{ color: 'var(--st-text)' }}>{item.label}</span>
                <span className="shrink-0 tabular-nums" style={{ color: 'var(--st-text-muted)' }}>
                  {item.caption || formatNumber(item.value)}
                </span>
              </div>
              <div
                className="h-1.5 overflow-hidden rounded-full"
                style={{ background: 'var(--st-surface-muted)' }}
              >
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.max(4, (item.value / max) * 100)}%`,
                    background: 'var(--st-accent)',
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function RunsTable({
  runs,
  selectedRunId,
  loading,
  onSelect,
}: {
  runs: ObservabilityRun[]
  selectedRunId: string
  loading: boolean
  onSelect: (runId: string) => void
}) {
  const [scrollTop, setScrollTop] = useState(0)
  const viewportHeight = 420
  const virtualized = runs.length >= VIRTUALIZE_OBSERVABILITY_THRESHOLD
  const startIndex = virtualized
    ? Math.max(0, Math.floor(scrollTop / RUN_ROW_HEIGHT_PX) - VIRTUAL_OVERSCAN)
    : 0
  const visibleCount = virtualized
    ? Math.ceil(viewportHeight / RUN_ROW_HEIGHT_PX) + VIRTUAL_OVERSCAN * 2
    : runs.length
  const visibleRuns = runs.slice(startIndex, startIndex + visibleCount)
  const topPadding = virtualized ? startIndex * RUN_ROW_HEIGHT_PX : 0
  const bottomPadding = virtualized
    ? Math.max(0, (runs.length - startIndex - visibleRuns.length) * RUN_ROW_HEIGHT_PX)
    : 0

  if (runs.length === 0 && !loading) {
    return (
      <div className="st-card px-4 py-10 text-center">
        <p className="st-desc mx-auto">No traces recorded yet.</p>
      </div>
    )
  }

  return (
    <div className="st-card">
      <div className="st-card-head flex items-center justify-between gap-3">
        <h4 className="st-label">Recent runs</h4>
        {!selectedRunId && runs.length > 0 && (
          <span className="st-hint">Select a run to inspect</span>
        )}
      </div>
      <div
        className="max-h-[420px] overflow-auto"
        onScroll={virtualized ? (event) => setScrollTop(event.currentTarget.scrollTop) : undefined}
      >
        {topPadding > 0 && <div aria-hidden style={{ height: topPadding }} />}
        {visibleRuns.map((run) => (
          <button
            key={run.run_id}
            type="button"
            aria-selected={selectedRunId === run.run_id}
            onClick={() => onSelect(run.run_id)}
            className="st-obs-row"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className={statusDotClass(run.status)} aria-hidden="true" />
                <span className="st-label truncate">{run.user_message || '(empty message)'}</span>
              </div>
              <div
                className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 pl-3.5 text-[11px]"
                style={{ color: 'var(--st-text-muted)' }}
              >
                <span>{run.status || '-'}</span>
                <span>·</span>
                <span>{run.source || '-'}</span>
                <span>·</span>
                <span className="truncate">{run.provider || '-'} / {run.model || '-'}</span>
                <span>·</span>
                <span className="font-mono" style={{ color: 'var(--st-text-faint)' }}>{shortId(run.run_id)}</span>
                {run.failure_reason && (
                  <>
                    <span>·</span>
                    <span className="truncate" style={{ color: 'var(--st-warn)' }}>{run.failure_reason}</span>
                  </>
                )}
              </div>
            </div>

            <div className="flex shrink-0 items-center gap-3 text-[11px] st-hint">
              <span>{formatDuration(run.duration_ms)}</span>
              <span>{formatNumber(run.total_tokens)} tok</span>
              <span style={run.tool_error_count ? { color: 'var(--st-warn)' } : undefined}>
                {run.tool_count}{run.tool_error_count ? `/${run.tool_error_count}` : ''} tools
              </span>
              <ChevronRight size={14} aria-hidden="true" />
            </div>
          </button>
        ))}
        {bottomPadding > 0 && <div aria-hidden style={{ height: bottomPadding }} />}
      </div>
    </div>
  )
}

function RunDetail({
  run,
  loading,
  actionRunId,
  onReplay,
  onExport,
  onClose,
}: {
  run: ObservabilityRunDetail | null
  loading: boolean
  actionRunId: string
  onReplay: (runId: string) => void
  onExport: (runId: string) => void
  onClose: () => void
}) {
  if (loading && !run) {
    return (
      <div className="st-card px-4 py-8 text-center">
        <p className="st-desc mx-auto">Loading run detail…</p>
      </div>
    )
  }
  if (!run) return null

  const toolEvents = run.events.filter((event) => event.event_type === 'tool_call_finished')

  return (
    <section aria-label="Run detail" className="st-card">
      <header className="st-card-head flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={statusTone(run.status)}>{run.status || '-'}</Badge>
            <span className="font-mono text-[11px]" style={{ color: 'var(--st-text-muted)' }}>{shortId(run.run_id)}</span>
            <span className="text-[11px]" style={{ color: 'var(--st-text-faint)' }}>{formatTime(run.started_at)}</span>
          </div>
          <p className="st-desc mt-1 truncate">{run.provider || '-'} / {run.model || '-'}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            onClick={() => onReplay(run.run_id)}
            disabled={!!actionRunId}
            className="st-btn st-btn-ghost st-btn-sm"
          >
            <Play size={13} className={actionRunId === run.run_id ? 'animate-pulse' : ''} />
            Replay
          </button>
          <button
            type="button"
            onClick={() => onExport(run.run_id)}
            disabled={!!actionRunId}
            className="st-btn st-btn-ghost st-btn-sm"
          >
            <Archive size={13} />
            Bundle
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close run detail"
            title="Close run detail"
            className="st-btn st-btn-ghost st-btn-icon st-btn-sm"
          >
            <X size={14} />
          </button>
        </div>
      </header>

      <div className="grid grid-cols-1 divide-y sm:grid-cols-3 sm:divide-x sm:divide-y-0" style={{ borderColor: 'var(--st-border)' }}>
        <CompactStat label="Duration" value={formatDuration(run.duration_ms)} />
        <CompactStat label="Tokens" value={`${formatNumber(run.total_tokens)} · ${run.usage_source || 'unknown'}`} />
        <CompactStat label="Cost" value={`${formatCost(run.estimated_cost_usd)} · ${run.cost_source || 'unknown'}`} />
      </div>

      <div className="st-divider-t space-y-4 p-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <TextBlock title="User" value={run.user_message} />
          <TextBlock title="Assistant" value={run.final_output} />
        </div>

        <div>
          <p className="st-hint mb-2 font-medium">Timeline</p>
          <div className="st-obs-timeline">
            {run.events.map((event) => (
              <div
                key={event.event_id}
                className="st-obs-timeline-item"
                data-tone={event.error_code || event.error_message ? 'error' : undefined}
              >
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-medium" style={{ color: 'var(--st-text)' }}>{event.event_type}</span>
                  {event.tool_name && <Badge>{event.tool_name}</Badge>}
                  {event.duration_ms ? <span className="st-hint">{formatDuration(event.duration_ms)}</span> : null}
                  {event.error_code && (
                    <span className="text-[11px]" style={{ color: 'var(--st-warn)' }}>{event.error_code}</span>
                  )}
                  <span className="ml-auto text-[11px]" style={{ color: 'var(--st-text-faint)' }}>
                    {formatTime(event.created_at)}
                  </span>
                </div>
                {event.error_message && (
                  <p className="mt-1 break-words text-[11px]" style={{ color: 'var(--st-warn)' }}>{event.error_message}</p>
                )}
              </div>
            ))}
          </div>
        </div>

        {toolEvents.length > 0 && (
          <div>
            <p className="st-hint mb-2 font-medium">Tool IO</p>
            <div className="overflow-hidden rounded-lg border" style={{ borderColor: 'var(--st-border)' }}>
              {toolEvents.map((event, index) => (
                <details
                  key={event.event_id}
                  className={`st-obs-disclosure ${index > 0 ? 'st-divider-t' : ''}`}
                >
                  <summary className="flex items-center justify-between gap-2 px-3 py-2 text-xs hover:bg-[var(--st-surface-hover)]">
                    <span style={{ color: 'var(--st-text)' }}>
                      {event.tool_name || 'tool'} · {event.status || '-'} · {formatDuration(event.duration_ms)}
                    </span>
                    <ChevronDown size={13} className="st-obs-disclosure-chevron shrink-0" style={{ color: 'var(--st-text-faint)' }} aria-hidden="true" />
                  </summary>
                  <div className="st-divider-t p-3">
                    <pre className="st-code max-h-56 overflow-auto whitespace-pre-wrap break-words">
                      {jsonText({ input: event.input, output: event.output })}
                    </pre>
                  </div>
                </details>
              ))}
            </div>
          </div>
        )}

        {run.errors.length > 0 && (
          <div>
            <p className="st-hint mb-2 font-medium">Errors</p>
            <ErrorsList errors={run.errors} />
          </div>
        )}
      </div>
    </section>
  )
}

function CompactStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-4 py-2.5" style={{ borderColor: 'var(--st-border)' }}>
      <p className="st-hint">{label}</p>
      <p className="mt-0.5 truncate text-xs font-medium tabular-nums" style={{ color: 'var(--st-text)' }}>{value}</p>
    </div>
  )
}

function TextBlock({ title, value }: { title: string; value: string }) {
  return (
    <div
      className="rounded-lg border p-3"
      style={{ borderColor: 'var(--st-border)', background: 'var(--st-surface-sunken)' }}
    >
      <p className="st-hint mb-1 font-medium">{title}</p>
      <p className="max-h-32 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed" style={{ color: 'var(--st-text)' }}>
        {value || '-'}
      </p>
    </div>
  )
}

function ErrorsList({
  errors,
  loading = false,
}: {
  errors: ObservabilityError[]
  loading?: boolean
}) {
  const [scrollTop, setScrollTop] = useState(0)
  const viewportHeight = 420
  const virtualized = errors.length >= VIRTUALIZE_OBSERVABILITY_THRESHOLD
  const startIndex = virtualized
    ? Math.max(0, Math.floor(scrollTop / ERROR_ROW_HEIGHT_PX) - VIRTUAL_OVERSCAN)
    : 0
  const visibleCount = virtualized
    ? Math.ceil(viewportHeight / ERROR_ROW_HEIGHT_PX) + VIRTUAL_OVERSCAN * 2
    : errors.length
  const visibleErrors = errors.slice(startIndex, startIndex + visibleCount)
  const topPadding = virtualized ? startIndex * ERROR_ROW_HEIGHT_PX : 0
  const bottomPadding = virtualized
    ? Math.max(0, (errors.length - startIndex - visibleErrors.length) * ERROR_ROW_HEIGHT_PX)
    : 0

  if (errors.length === 0 && !loading) {
    return (
      <div className="st-card px-4 py-10 text-center">
        <p className="st-desc mx-auto">No structured errors recorded.</p>
      </div>
    )
  }

  return (
    <div
      className={`st-card ${virtualized ? 'max-h-[420px] overflow-auto' : ''}`}
      onScroll={virtualized ? (event) => setScrollTop(event.currentTarget.scrollTop) : undefined}
    >
      {topPadding > 0 && <div aria-hidden style={{ height: topPadding }} />}
      {visibleErrors.map((item, index) => (
        <details
          key={item.error_id}
          className={`st-obs-disclosure ${index > 0 ? 'st-divider-t' : ''}`}
        >
          <summary className="flex items-center gap-2 px-3.5 py-2.5 text-xs hover:bg-[var(--st-surface-hover)]">
            <Badge tone="danger">{item.level || 'error'}</Badge>
            <span className="shrink-0 font-medium" style={{ color: 'var(--st-text)' }}>
              {item.error_type || item.logger_name || 'error'}
            </span>
            <span className="min-w-0 flex-1 truncate" style={{ color: 'var(--st-text-muted)' }}>
              {item.message}
            </span>
            <span className="shrink-0 text-[11px]" style={{ color: 'var(--st-text-faint)' }}>
              {formatTime(item.created_at)}
            </span>
          </summary>
          <div className="st-divider-t p-3">
            <pre className="st-code max-h-72 overflow-auto whitespace-pre-wrap break-words">
              {item.traceback || jsonText(item.metadata)}
            </pre>
          </div>
        </details>
      ))}
      {bottomPadding > 0 && <div aria-hidden style={{ height: bottomPadding }} />}
    </div>
  )
}

function BackendLogView({
  log,
  loading,
  supportModeRequired,
  enabling,
  onEnableSupportMode,
}: {
  log: ObservabilityBackendLog | null
  loading: boolean
  supportModeRequired: boolean
  enabling: boolean
  onEnableSupportMode: () => void
}) {
  if (supportModeRequired) {
    return (
      <div className="st-card">
        <div className="p-6 text-center">
          <p className="st-label">Support mode is off</p>
          <p className="st-desc mx-auto mt-2">
            The raw backend log can contain file paths and request details, so it stays locked until you
            unlock it. Turning support mode on grants access for 10 minutes, then it locks itself again.
          </p>
          <button
            type="button"
            onClick={onEnableSupportMode}
            disabled={enabling}
            className="st-btn st-btn-primary mx-auto mt-4"
          >
            {enabling ? 'Unlocking…' : 'Unlock for 10 minutes'}
          </button>
        </div>
      </div>
    )
  }
  if (loading && !log) {
    return <div className="st-card"><p className="st-desc p-10 text-center">Loading backend log…</p></div>
  }
  if (!log || !log.exists) {
    return <div className="st-card"><p className="st-desc p-10 text-center">backend.log has not been written yet.</p></div>
  }
  return (
    <div className="st-card">
      <div className="st-card-head flex flex-wrap items-center justify-between gap-2 text-xs">
        <div className="flex min-w-0 items-center gap-2" style={{ color: 'var(--st-text-muted)' }}>
          <Clock3 size={13} className="shrink-0" aria-hidden="true" />
          <span className="truncate font-mono text-[11px]">{log.path}</span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="st-hint">{formatNumber(log.lines.length)} line{log.lines.length === 1 ? '' : 's'}</span>
          {log.truncated && <Badge tone="warn">Tail truncated</Badge>}
        </div>
      </div>
      <div className="p-3">
        <pre className="st-code max-h-[460px] overflow-auto whitespace-pre-wrap break-words">
          {log.lines.join('\n') || '(empty)'}
        </pre>
      </div>
    </div>
  )
}
