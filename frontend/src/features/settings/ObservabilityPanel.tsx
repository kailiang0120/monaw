import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Archive,
  Bug,
  Clock3,
  Coins,
  FileText,
  Gauge,
  Play,
  RefreshCw,
  Search,
  Timer,
  Wrench,
  type LucideIcon,
} from 'lucide-react'
import {
  exportObservabilityDebugBundle,
  fetchBackendLogTail,
  fetchObservabilityErrors,
  fetchObservabilityRun,
  fetchObservabilityRuns,
  fetchObservabilitySummary,
  replayObservabilityRun,
} from '../../lib/api/diagnostics'
import type {
  ObservabilityBackendLog,
  ObservabilityError,
  ObservabilityRun,
  ObservabilityRunDetail,
  ObservabilitySummary,
} from '../../lib/api/types'

type ViewMode = 'runs' | 'errors' | 'backend'

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
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat().format(Math.round(value || 0))
}

function formatCost(value: number): string {
  if (!value) return '$0.00'
  return `$${value.toFixed(value < 0.01 ? 5 : 2)}`
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

function statusClass(status: string): string {
  const normalized = status.toLowerCase()
  if (normalized === 'complete') return 'border-emerald-400/20 bg-emerald-400/10 text-emerald-200'
  if (normalized === 'running') return 'border-accent/30 bg-accent/10 text-accent-light'
  if (normalized === 'paused') return 'border-amber-400/20 bg-amber-400/10 text-amber-200'
  return 'border-red-400/20 bg-red-400/10 text-red-200'
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

export function ObservabilityPanel() {
  const [summary, setSummary] = useState<ObservabilitySummary>(EMPTY_SUMMARY)
  const [runs, setRuns] = useState<ObservabilityRun[]>([])
  const [errors, setErrors] = useState<ObservabilityError[]>([])
  const [backendLog, setBackendLog] = useState<ObservabilityBackendLog | null>(null)
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedRun, setSelectedRun] = useState<ObservabilityRunDetail | null>(null)
  const [view, setView] = useState<ViewMode>('runs')
  const [statusFilter, setStatusFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [actionRunId, setActionRunId] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const loadSummary = async () => {
    setSummary(await fetchObservabilitySummary())
  }

  const loadRuns = async () => {
    setRuns(await fetchObservabilityRuns({
      status: statusFilter,
      source: sourceFilter,
      q: query,
      limit: 100,
    }))
  }

  const loadErrors = async () => {
    setErrors(await fetchObservabilityErrors({ q: query, limit: 100 }))
  }

  const loadBackendLog = async () => {
    setBackendLog(await fetchBackendLogTail(500))
  }

  const refreshAll = async () => {
    setLoading(true)
    setError('')
    try {
      await Promise.all([loadSummary(), loadRuns(), loadErrors(), loadBackendLog()])
      if (selectedRunId) {
        setSelectedRun(await fetchObservabilityRun(selectedRunId))
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Failed to load observability data')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refreshAll()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    void (async () => {
      try {
        if (view === 'runs') {
          await loadRuns()
          return
        }
        if (view === 'errors') {
          await loadErrors()
        }
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : 'Failed to apply filters')
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, sourceFilter, query, view])

  useEffect(() => {
    if (view !== 'backend') return
    void (async () => {
      try {
        await loadBackendLog()
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : 'Failed to load backend log')
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view])

  const selectRun = async (runId: string) => {
    setSelectedRunId(runId)
    setDetailLoading(true)
    setError('')
    try {
      setSelectedRun(await fetchObservabilityRun(runId))
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Failed to load run detail')
    } finally {
      setDetailLoading(false)
    }
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
      setNotice(`Debug bundle exported to ${result.path}.`)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Export failed')
    } finally {
      setActionRunId('')
    }
  }

  const trendMax = useMemo(() => {
    return Math.max(1, ...summary.token_usage_over_time.map((item) => Number(item.tokens || 0)))
  }, [summary.token_usage_over_time])

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-neutral-200">Runtime traces, token usage, local errors, and replay diagnosis.</p>
          {summary.storage_path && (
            <p className="mt-1 truncate text-[11px] text-neutral-600">{summary.storage_path}</p>
          )}
        </div>
        <button
          type="button"
          onClick={() => void refreshAll()}
          disabled={loading}
          className="ghost-button h-8 rounded-lg px-3 text-xs disabled:opacity-50"
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-400/20 bg-red-400/10 px-3 py-2 text-xs text-red-200">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-3 py-2 text-xs text-emerald-200">
          {notice}
        </div>
      )}

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Metric icon={Activity} label="Total runs" value={formatNumber(summary.total_runs)} />
        <Metric icon={Gauge} label="Success rate" value={`${Math.round((summary.success_rate || 0) * 100)}%`} />
        <Metric icon={AlertTriangle} label="Failed runs" value={formatNumber(summary.failed_runs)} tone={summary.failed_runs ? 'amber' : 'default'} />
        <Metric icon={Timer} label="Avg duration" value={formatDuration(summary.average_duration_ms)} />
        <Metric icon={FileText} label="Total tokens" value={formatNumber(summary.total_tokens)} />
        <Metric icon={Coins} label="Estimated cost" value={formatCost(summary.estimated_cost_usd)} />
        <Metric icon={Wrench} label="Tool errors" value={formatNumber(summary.tool_error_count)} tone={summary.tool_error_count ? 'amber' : 'default'} />
        <Metric icon={Bug} label="Error patterns" value={formatNumber(summary.top_error_reasons.length)} />
      </div>

      <div className="grid gap-3 xl:grid-cols-5">
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
          max={Math.max(1, ...summary.duration_trend.map((item) => Number(item.average_duration_ms || 0)))}
        />
        <RankList title="Top error reasons" items={summary.top_error_reasons.map((item) => ({ label: item.reason, value: item.count }))} />
        <RankList title="Failing tools" items={summary.top_failing_tools.map((item) => ({ label: item.tool_name || 'unknown', value: item.count }))} />
        <RankList title="Model usage" items={summary.model_usage.map((item) => ({ label: `${item.provider}/${item.model}`, value: item.runs, caption: `${formatNumber(item.tokens)} tokens` }))} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {(['runs', 'errors', 'backend'] as ViewMode[]).map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setView(item)}
            className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
              view === item
                ? 'border-accent bg-accent text-white'
                : 'border-white/[0.08] bg-white/[0.03] text-neutral-400 hover:text-neutral-100'
            }`}
          >
            {item === 'runs' ? 'Runs' : item === 'errors' ? 'Errors' : 'Backend log'}
          </button>
        ))}
        <div className="ml-auto flex min-w-[220px] items-center gap-2 rounded-lg border border-white/[0.08] bg-black/10 px-2 py-1.5">
          <Search size={13} className="text-neutral-500" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search traces"
            className="min-w-0 flex-1 bg-transparent text-xs text-neutral-200 outline-none placeholder:text-neutral-600"
          />
        </div>
        {view === 'runs' && (
          <>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="control h-8 rounded-lg px-2 text-xs">
              <option value="">All status</option>
              <option value="complete">Complete</option>
              <option value="paused">Paused</option>
              <option value="error">Error</option>
              <option value="running">Running</option>
            </select>
            <select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)} className="control h-8 rounded-lg px-2 text-xs">
              <option value="">All sources</option>
              <option value="desktop">Desktop</option>
              <option value="telegram">Telegram</option>
              <option value="scheduled">Scheduled</option>
              <option value="replay">Replay</option>
            </select>
          </>
        )}
      </div>

      {view === 'runs' && (
        <div className="grid gap-3 xl:grid-cols-[minmax(0,1.05fr)_minmax(360px,0.95fr)]">
          <RunsTable
            runs={runs}
            selectedRunId={selectedRunId}
            loading={loading}
            onSelect={(runId) => void selectRun(runId)}
          />
          <RunDetail
            run={selectedRun}
            loading={detailLoading}
            actionRunId={actionRunId}
            onReplay={(runId) => void replayRun(runId)}
            onExport={(runId) => void exportBundle(runId)}
          />
        </div>
      )}

      {view === 'errors' && <ErrorsList errors={errors} loading={loading} />}
      {view === 'backend' && <BackendLogView log={backendLog} loading={loading} />}
    </div>
  )
}

function Metric({
  icon: Icon,
  label,
  value,
  tone = 'default',
}: {
  icon: LucideIcon
  label: string
  value: string
  tone?: 'default' | 'amber'
}) {
  return (
    <div className={`rounded-lg border px-3 py-2 ${
      tone === 'amber'
        ? 'border-amber-400/20 bg-amber-400/10'
        : 'border-white/[0.07] bg-black/10'
    }`}>
      <div className="flex items-center gap-2">
        <Icon size={13} className="text-neutral-500" />
        <p className="section-label">{label}</p>
      </div>
      <p className="mt-1 text-lg font-semibold text-neutral-100">{value}</p>
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
    <div className="panel-muted rounded-xl p-3">
      <p className="section-label mb-3">{title}</p>
      {items.length === 0 ? (
        <p className="py-6 text-center text-xs text-neutral-600">No data yet.</p>
      ) : (
        <div className="flex h-28 items-end gap-1.5">
          {items.map((item) => (
            <div key={item.label} className="flex min-w-0 flex-1 flex-col items-center gap-1">
              <div className="flex h-20 w-full items-end rounded bg-black/10">
                <div
                  className="w-full rounded bg-accent/70"
                  style={{ height: `${Math.max(4, (item.value / max) * 100)}%` }}
                  title={`${item.label}: ${formatNumber(item.value)} ${item.caption || ''}`}
                />
              </div>
              <span className="truncate text-[10px] text-neutral-600">{item.label}</span>
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
    <div className="panel-muted rounded-xl p-3">
      <p className="section-label mb-3">{title}</p>
      {items.length === 0 ? (
        <p className="py-6 text-center text-xs text-neutral-600">No data yet.</p>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <div key={item.label} className="space-y-1">
              <div className="flex items-center justify-between gap-2 text-xs">
                <span className="min-w-0 truncate text-neutral-300">{item.label}</span>
                <span className="shrink-0 text-neutral-500">{item.caption || formatNumber(item.value)}</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-black/15">
                <div className="h-full rounded-full bg-accent/70" style={{ width: `${Math.max(4, (item.value / max) * 100)}%` }} />
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
  if (runs.length === 0 && !loading) {
    return <div className="panel-muted rounded-xl px-4 py-10 text-center text-sm text-neutral-500">No traces recorded yet.</div>
  }
  return (
    <div className="overflow-hidden rounded-xl border border-white/[0.07]">
      <div className="grid grid-cols-[84px_88px_1fr_84px_70px] border-b border-white/[0.07] bg-black/10 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-neutral-600">
        <span>Status</span>
        <span>Source</span>
        <span>Message</span>
        <span>Tokens</span>
        <span>Tools</span>
      </div>
      <div className="max-h-[520px] overflow-auto">
        {runs.map((run) => (
          <button
            key={run.run_id}
            type="button"
            onClick={() => onSelect(run.run_id)}
            className={`grid w-full grid-cols-[84px_88px_1fr_84px_70px] gap-2 border-b border-white/[0.06] px-3 py-2 text-left text-xs transition-colors hover:bg-white/[0.03] ${
              selectedRunId === run.run_id ? 'bg-accent/10' : ''
            }`}
          >
            <span className={`status-pill w-fit ${statusClass(run.status)}`}>{run.status || '-'}</span>
            <span className="text-neutral-500">{run.source || '-'}</span>
            <span className="min-w-0">
              <span className="block truncate text-neutral-200">{run.user_message || '(empty message)'}</span>
              <span className="block truncate text-[11px] text-neutral-600">
                {shortId(run.run_id)} · {run.provider || '-'} / {run.model || '-'} · {formatDuration(run.duration_ms)}
              </span>
              {run.failure_reason && <span className="block truncate text-[11px] text-amber-300">{run.failure_reason}</span>}
            </span>
            <span className="text-neutral-500">{formatNumber(run.total_tokens)}</span>
            <span className={run.tool_error_count ? 'text-amber-300' : 'text-neutral-500'}>
              {run.tool_count}{run.tool_error_count ? `/${run.tool_error_count}` : ''}
            </span>
          </button>
        ))}
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
}: {
  run: ObservabilityRunDetail | null
  loading: boolean
  actionRunId: string
  onReplay: (runId: string) => void
  onExport: (runId: string) => void
}) {
  if (loading) {
    return <div className="panel-muted rounded-xl px-4 py-10 text-center text-sm text-neutral-500">Loading run detail...</div>
  }
  if (!run) {
    return <div className="panel-muted rounded-xl px-4 py-10 text-center text-sm text-neutral-500">Select a run to inspect the timeline.</div>
  }
  const toolEvents = run.events.filter((event) => event.event_type === 'tool_call_finished')
  return (
    <div className="panel-muted max-h-[620px] overflow-auto rounded-xl p-3">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`status-pill ${statusClass(run.status)}`}>{run.status || '-'}</span>
            <span className="font-mono text-[11px] text-neutral-500">{shortId(run.run_id)}</span>
            <span className="text-[11px] text-neutral-600">{formatTime(run.started_at)}</span>
          </div>
          <p className="mt-1 truncate text-xs text-neutral-300">{run.provider || '-'} / {run.model || '-'}</p>
        </div>
        <div className="flex gap-2">
          <button type="button" onClick={() => onReplay(run.run_id)} disabled={!!actionRunId} className="ghost-button h-8 rounded-lg px-2.5 text-xs disabled:opacity-50">
            <Play size={13} className={actionRunId === run.run_id ? 'animate-pulse' : ''} />
            Replay
          </button>
          <button type="button" onClick={() => onExport(run.run_id)} disabled={!!actionRunId} className="ghost-button h-8 rounded-lg px-2.5 text-xs disabled:opacity-50">
            <Archive size={13} />
            Bundle
          </button>
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        <CompactStat label="Duration" value={formatDuration(run.duration_ms)} />
        <CompactStat label="Tokens" value={`${formatNumber(run.total_tokens)} · ${run.usage_source || 'unknown'}`} />
        <CompactStat label="Cost" value={`${formatCost(run.estimated_cost_usd)} · ${run.cost_source || 'unknown'}`} />
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <TextBlock title="User" value={run.user_message} />
        <TextBlock title="Assistant" value={run.final_output} />
      </div>

      <div className="mt-3">
        <p className="section-label mb-2">Timeline</p>
        <div className="space-y-2">
          {run.events.map((event) => (
            <div key={event.event_id} className="rounded-lg border border-white/[0.07] bg-black/10 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium text-neutral-200">{event.event_type}</span>
                {event.tool_name && <span className="status-pill border-white/[0.08] bg-white/[0.03] text-neutral-400">{event.tool_name}</span>}
                {event.duration_ms ? <span className="text-neutral-500">{formatDuration(event.duration_ms)}</span> : null}
                {event.error_code && <span className="text-amber-300">{event.error_code}</span>}
                <span className="ml-auto text-[11px] text-neutral-600">{formatTime(event.created_at)}</span>
              </div>
              {event.error_message && <p className="mt-1 break-words text-[11px] text-amber-300">{event.error_message}</p>}
            </div>
          ))}
        </div>
      </div>

      {toolEvents.length > 0 && (
        <div className="mt-3">
          <p className="section-label mb-2">Tool IO</p>
          <div className="space-y-2">
            {toolEvents.map((event) => (
              <details key={event.event_id} className="rounded-lg border border-white/[0.07] bg-black/10 px-3 py-2">
                <summary className="cursor-pointer text-xs text-neutral-200">
                  {event.tool_name || 'tool'} · {event.status || '-'} · {formatDuration(event.duration_ms)}
                </summary>
                <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-neutral-400">
                  {jsonText({ input: event.input, output: event.output })}
                </pre>
              </details>
            ))}
          </div>
        </div>
      )}

      {run.errors.length > 0 && (
        <div className="mt-3">
          <p className="section-label mb-2">Errors</p>
          <ErrorsList errors={run.errors} />
        </div>
      )}
    </div>
  )
}

function CompactStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-white/[0.07] bg-black/10 px-2.5 py-2">
      <p className="section-label">{label}</p>
      <p className="mt-1 truncate text-xs font-medium text-neutral-200">{value}</p>
    </div>
  )
}

function TextBlock({ title, value }: { title: string; value: string }) {
  return (
    <div className="rounded-lg border border-white/[0.07] bg-black/10 px-3 py-2">
      <p className="section-label mb-1">{title}</p>
      <p className="max-h-32 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-neutral-300">{value || '-'}</p>
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
  if (errors.length === 0 && !loading) {
    return <div className="panel-muted rounded-xl px-4 py-10 text-center text-sm text-neutral-500">No structured errors recorded.</div>
  }
  return (
    <div className="space-y-2">
      {errors.map((item) => (
        <details key={item.error_id} className="rounded-xl border border-red-400/15 bg-red-400/5 px-3 py-2">
          <summary className="cursor-pointer">
            <div className="inline-flex max-w-full flex-wrap items-center gap-2 text-xs">
              <span className="status-pill border-red-400/20 bg-red-400/10 text-red-200">{item.level || 'error'}</span>
              <span className="font-medium text-neutral-200">{item.error_type || item.logger_name || 'error'}</span>
              <span className="truncate text-neutral-500">{item.message}</span>
              <span className="text-[11px] text-neutral-600">{formatTime(item.created_at)}</span>
            </div>
          </summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-neutral-400">
            {item.traceback || jsonText(item.metadata)}
          </pre>
        </details>
      ))}
    </div>
  )
}

function BackendLogView({ log, loading }: { log: ObservabilityBackendLog | null; loading: boolean }) {
  if (loading && !log) {
    return <div className="panel-muted rounded-xl px-4 py-10 text-center text-sm text-neutral-500">Loading backend log...</div>
  }
  if (!log || !log.exists) {
    return <div className="panel-muted rounded-xl px-4 py-10 text-center text-sm text-neutral-500">backend.log is not available yet.</div>
  }
  return (
    <div className="panel-muted rounded-xl p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-neutral-500">
        <Clock3 size={13} />
        <span className="truncate">{log.path}</span>
        {log.truncated && <span className="text-amber-300">tail truncated</span>}
      </div>
      <pre className="max-h-[560px] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-white/[0.07] bg-black/20 p-3 text-[11px] leading-relaxed text-neutral-400">
        {log.lines.join('\n') || '(empty)'}
      </pre>
    </div>
  )
}
