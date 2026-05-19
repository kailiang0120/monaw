import { useEffect, useMemo, useState } from 'react'
import { Play, RefreshCw, RotateCcw } from 'lucide-react'
import { fetchEvaluationRuns, replayEvaluationRun } from '../../lib/api/diagnostics'
import type { EvaluationReplayResult, EvaluationRun } from '../../lib/api/types'

function formatTime(value: string): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function shortId(value: string): string {
  return value ? value.slice(0, 10) : '-'
}

export function EvaluationReplayPanel() {
  const [runs, setRuns] = useState<EvaluationRun[]>([])
  const [loading, setLoading] = useState(false)
  const [replaying, setReplaying] = useState('')
  const [lastReplay, setLastReplay] = useState<EvaluationReplayResult | null>(null)
  const [error, setError] = useState('')

  const stats = useMemo(() => {
    const failed = runs.filter((run) => !run.success).length
    const toolCalls = runs.reduce((total, run) => total + run.tool_count, 0)
    return { failed, toolCalls }
  }, [runs])

  const loadRuns = async () => {
    setLoading(true)
    setError('')
    try {
      setRuns(await fetchEvaluationRuns(50))
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Failed to load evaluation runs')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadRuns()
  }, [])

  const runReplay = async (runId: string) => {
    setReplaying(runId)
    setLastReplay(null)
    setError('')
    try {
      setLastReplay(await replayEvaluationRun(runId))
      await loadRuns()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Replay failed')
    } finally {
      setReplaying('')
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Metric label="Runs" value={runs.length} />
        <Metric label="Failed" value={stats.failed} tone={stats.failed ? 'amber' : 'default'} />
        <Metric label="Tool calls" value={stats.toolCalls} />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-neutral-500">
          Recorded turns can be replayed into a fresh conversation. Successful runs are kept for 1 day; failed runs are kept for 3 days.
        </p>
        <button
          type="button"
          onClick={() => void loadRuns()}
          disabled={loading}
          className="ghost-button rounded-xl px-3 py-2 text-xs disabled:opacity-50"
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

      {lastReplay && (
        <div className="rounded-lg border border-emerald-400/20 bg-emerald-400/10 px-3 py-2 text-xs text-emerald-200">
          Replay created conversation {lastReplay.conversation_id} with status {lastReplay.status}.
        </div>
      )}

      <div className="space-y-2">
        {runs.length === 0 && !loading ? (
          <div className="panel-muted rounded-xl px-4 py-8 text-center text-sm text-neutral-500">
            No evaluation runs recorded yet.
          </div>
        ) : (
          runs.map((run) => (
            <div key={run.run_id} className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    <span className={`status-pill ${
                      run.success
                        ? 'border-emerald-400/20 bg-emerald-400/10 text-emerald-200'
                        : 'border-amber-400/20 bg-amber-400/10 text-amber-200'
                    }`}>
                      {run.success ? 'success' : 'failed'}
                    </span>
                    <span className="font-mono text-[11px] text-neutral-500">{shortId(run.run_id)}</span>
                    <span className="text-[11px] text-neutral-600">{formatTime(run.finished_at || run.started_at)}</span>
                  </div>
                  <p className="max-h-10 overflow-hidden break-words text-xs leading-relaxed text-neutral-300">
                    {run.last_user_message || '(empty user message)'}
                  </p>
                  {run.error && <p className="mt-1 text-[11px] text-amber-300">{run.error}</p>}
                </div>
                <button
                  type="button"
                  onClick={() => void runReplay(run.run_id)}
                  disabled={!!replaying}
                  className="ghost-button rounded-xl px-3 py-2 text-xs disabled:opacity-50"
                >
                  {replaying === run.run_id ? <RotateCcw size={13} className="animate-spin" /> : <Play size={13} />}
                  Replay
                </button>
              </div>
              <div className="mt-2 grid gap-2 text-[11px] text-neutral-600 sm:grid-cols-4">
                <span>Model: {run.provider || '-'} / {run.model || '-'}</span>
                <span>Tools: {run.tool_count}</span>
                <span>Entries: {run.entry_count}</span>
                <span>Conversation: {shortId(run.conversation_id)}</span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

function Metric({
  label,
  value,
  tone = 'default',
}: {
  label: string
  value: number
  tone?: 'default' | 'amber'
}) {
  return (
    <div className={`rounded-xl border px-3 py-2 ${
      tone === 'amber'
        ? 'border-amber-400/20 bg-amber-400/10'
        : 'border-white/[0.07] bg-black/10'
    }`}>
      <p className="section-label">{label}</p>
      <p className="mt-1 text-lg font-semibold text-neutral-100">{value}</p>
    </div>
  )
}
