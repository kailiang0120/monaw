import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowLeft, FileDiff, RefreshCw, Undo2 } from 'lucide-react'
import type { RunSnapshot } from '../lib/api/runs'
import { applyGitUndo, fetchGitChanges, previewGitUndo, type GitChange, type GitChanges } from '../lib/api/gitChanges'
import { useModalFocus } from '../hooks/useModalFocus'
import { UnifiedDiff } from './UnifiedDiff'
import { Dropdown } from './Dropdown'

const undoReason: Record<string, string> = {
  undone: 'Undone', conflict: 'Changed since this run · Undo unavailable',
  overlapping_runs: 'Overlapping runs · review changes manually',
  needs_review: 'Undo interrupted · review the file', unsupported: 'Undo unavailable on this platform',
  unavailable: 'Undo unavailable for this file',
}

function Counts({ added, removed }: { added: number | null; removed: number | null }) {
  return added === null ? <span>Binary file</span> : <span className="shrink-0 tabular-nums"><span className="text-emerald-300">+{added.toLocaleString()}</span> <span className="text-red-200">−{(removed || 0).toLocaleString()}</span></span>
}

export function ChangesReview({ conversationId, runs, snapshot, legacyUndo }: {
  conversationId: string; runs: RunSnapshot[]; snapshot: RunSnapshot | null; legacyUndo?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState('')
  const runId = selected || snapshot?.run_id || ''
  const scopeKey = `${conversationId}/${runId}`
  const currentScope = useRef(scopeKey)
  currentScope.current = scopeKey
  const run = runs.find(item => item.run_id === runId) || (snapshot?.run_id === runId ? snapshot : null)
  const [data, setData] = useState<GitChanges | null>(null)
  const [filePath, setFilePath] = useState('')
  const [detail, setDetail] = useState<GitChange | null>(null)
  const [preview, setPreview] = useState<{ confirmation_id: string; file: GitChange } | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const dialogRef = useRef<HTMLElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  useModalFocus(dialogRef, { remountKey: open, onEscape: () => {
    if (!busy) { if (preview) setPreview(null); else setOpen(false) }
  } })
  useEffect(() => { if (preview) cancelRef.current?.focus() }, [preview])
  useEffect(() => {
    setDetail(null); setPreview(null); setData(null)
    if (!runId) return
    const controller = new AbortController()
    void fetchGitChanges(runId, conversationId, undefined, controller.signal).then(result => {
      if (!controller.signal.aborted) {
        setData(result)
        setFilePath(current => result.files.some(file => file.path === current) ? current : result.files[0]?.path || '')
      }
    }).catch(err => { if (!controller.signal.aborted) setError(String(err.message || err)) })
    return () => controller.abort()
  }, [runId, run?.revision, conversationId, refresh])
  useEffect(() => {
    setDetail(null); setPreview(null)
    if (!open || !filePath || !data?.files.some(file => file.path === filePath)) return
    const controller = new AbortController()
    void fetchGitChanges(runId, conversationId, filePath, controller.signal).then(result => {
      if (!controller.signal.aborted) setDetail(result.files[0] || null)
    }).catch(err => { if (!controller.signal.aborted) setError(String(err.message || err)) })
    return () => controller.abort()
  }, [open, runId, conversationId, filePath, data])
  const totals = data?.files.reduce((sum, file) => ({ added: sum.added + (file.added || 0), removed: sum.removed + (file.removed || 0) }), { added: 0, removed: 0 })
  const refreshChanges = () => { setError(''); setRefresh(value => value + 1) }

  return <>
    <button type="button" aria-label="Changes" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}
      className="no-drag mr-2 flex min-h-8 items-center gap-2 rounded-lg border border-white/[0.08] px-3 text-xs text-neutral-300 hover:bg-white/5">
      <FileDiff size={15} /> Changes
      {totals && !data?.unavailable && <span className="hidden sm:inline"><Counts {...totals} /></span>}
    </button>
    {open && <section ref={dialogRef} role="dialog" aria-modal="true" aria-label="Workspace changes" tabIndex={-1}
      className="below-app-bar no-drag fixed inset-0 z-40 flex select-text flex-col bg-[var(--st-bg-nav)] text-sm font-normal text-neutral-300">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-white/[0.08] px-5 py-3">
        <button type="button" disabled={busy} onClick={() => { setOpen(false); setPreview(null) }} className="flex min-h-9 items-center gap-2 rounded-lg px-2 hover:bg-white/5"><ArrowLeft size={17} /> Back to chat</button>
        <div className="flex min-w-0 items-center gap-3">
          <Dropdown
            ariaLabel="Changes for run"
            value={runId}
            disabled={busy}
            onChange={value => { setSelected(value); setError('') }}
            options={[...runs].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((item, index) => ({
              value: item.run_id,
              label: `${index === 0 ? 'Latest run' : new Date(item.created_at).toLocaleString()} · ${item.status}`,
            }))}
            placeholder="Select a run"
            size="sm"
            align="right"
            className="min-w-0 max-w-64"
          />
          {totals && !data?.unavailable && <Counts {...totals} />}
          <button type="button" aria-label="Refresh changes" disabled={busy} onClick={refreshChanges} className="rounded-lg p-2 hover:bg-white/5"><RefreshCw size={16} /></button>
        </div>
      </header>
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <nav aria-label="Changed files" className="max-h-48 shrink-0 overflow-y-auto border-b border-white/[0.08] p-4 md:max-h-none md:w-72 md:border-b-0 md:border-r">
          <h2 className="mb-1 font-medium">Changes {data && !data.unavailable && <span className="text-neutral-500">· {data.files.length} {data.files.length === 1 ? 'file' : 'files'}</span>}</h2>
          <p className="mb-4 text-xs leading-relaxed text-neutral-500">Workspace changes during this run, tracked with Git.</p>
          {data?.files.map(file => <button key={file.path} type="button" disabled={busy} aria-current={filePath === file.path ? 'true' : undefined}
            onClick={() => { setFilePath(file.path); setError('') }}
            className={`mb-1 flex w-full flex-col gap-1 rounded-lg px-3 py-2 text-left text-xs ${filePath === file.path ? 'bg-accent/10 text-neutral-200' : 'hover:bg-white/5'}`}>
            <span className="break-all font-mono">{file.path}</span><Counts added={file.added} removed={file.removed} />
          </button>)}
          {Boolean(data?.excluded_count) && <p className="mt-3 text-xs text-amber-200">{data?.excluded_count} files could not be tracked. Ignored files are excluded.</p>}
        </nav>
        <main className="min-w-0 flex-1 overflow-y-auto p-4 md:p-6">
          {error && <p role="alert" className="mb-4 text-red-200">{error}</p>}
          {!runId ? <p>No run selected.</p> : !data && !error ? <p>Loading changes…</p> : null}
          {data?.unavailable && <><p className="mb-4 text-neutral-400">{data.unavailable}</p>{runId === snapshot?.run_id && legacyUndo}</>}
          {data && !data.unavailable && data.files.length === 0 && <p>No tracked files changed during this run.</p>}
          {detail && !preview && <>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <h3 className="min-w-0 break-all font-mono text-xs">{detail.path}</h3>
              {detail.undo === 'available' ? <button type="button" disabled={busy || !['completed', 'failed', 'cancelled'].includes(run?.status || '')}
                className="flex items-center gap-2 rounded-lg border border-red-400/30 px-3 py-2 text-xs text-red-200 disabled:opacity-50"
                onClick={async () => {
                  setBusy(true); setError('')
                  try { const result = await previewGitUndo(runId, conversationId, detail.path); if (currentScope.current === scopeKey) setPreview(result) }
                  catch (err) { if (currentScope.current === scopeKey) setError(err instanceof Error ? err.message : 'Could not preview Undo') }
                  finally { setBusy(false) }
                }}><Undo2 size={14} /> Undo</button> : <span className="text-xs text-neutral-500">{undoReason[detail.undo] || 'Undo unavailable'}</span>}
            </div>
            <UnifiedDiff compact={false} label="File changes" text={detail.diff || 'No text diff available.'} />
            {detail.truncated && <p className="mt-2 text-xs text-neutral-500">Diff truncated.</p>}
          </>}
          {preview && <section aria-label="Confirm Git Undo" className="rounded-xl border border-red-400/30 p-4">
            <h3 className="mb-2 font-medium">Review what Undo will change</h3>
            <p className="mb-2 break-all font-mono text-xs">{preview.file.path}</p>
            <p className="mb-4 text-xs text-neutral-400">Restore this file to its state before this run. Your earlier uncommitted edits are preserved.</p>
            <UnifiedDiff compact={false} label="Undo diff" text={preview.file.diff || 'No text diff available.'} />
            <div className="mt-4 flex gap-2">
              <button type="button" disabled={busy} className="rounded-lg border border-red-400/30 bg-red-400/10 px-4 py-2 text-xs text-red-200 disabled:opacity-50" onClick={async () => {
                setBusy(true); setError('')
                try { await applyGitUndo(runId, conversationId, preview.file.path, preview.confirmation_id); if (currentScope.current === scopeKey) { setPreview(null); setRefresh(value => value + 1) } }
                catch (err) { if (currentScope.current === scopeKey) { setError(err instanceof Error ? err.message : 'Could not undo change'); setPreview(null); setRefresh(value => value + 1) } }
                finally { setBusy(false) }
              }}>Undo</button>
              <button ref={cancelRef} type="button" disabled={busy} onClick={() => setPreview(null)} className="rounded-lg border border-white/10 px-4 py-2 text-xs">Cancel</button>
            </div>
          </section>}
        </main>
      </div>
    </section>}
  </>
}
