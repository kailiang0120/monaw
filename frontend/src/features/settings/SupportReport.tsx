import { useState } from 'react'
import { Download, FileText, Loader2 } from 'lucide-react'

import { fetchSupportReport } from '../../lib/api/diagnostics'

const FILE_NAME = 'monaw-support-report.json'

function formatSize(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`
}

/**
 * Prepare a support report, show all of it, and only then let the user save it.
 * Keys, paths and setting values are redacted by the backend; the user still decides what to share.
 */
export function SupportReport() {
  const [text, setText] = useState('')
  const [logIncluded, setLogIncluded] = useState(false)
  const [truncated, setTruncated] = useState(false)
  const [preparing, setPreparing] = useState(false)
  const [error, setError] = useState('')

  const prepare = async () => {
    setPreparing(true)
    setError('')
    try {
      const report = await fetchSupportReport()
      const log = report.backend_log as { included?: boolean } | undefined
      setLogIncluded(Boolean(log?.included))
      setTruncated(Boolean(report.truncated))
      setText(JSON.stringify(report, null, 2))
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Failed to prepare the support report')
    } finally {
      setPreparing(false)
    }
  }

  const save = () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = FILE_NAME
    link.click()
    URL.revokeObjectURL(url)
  }

  const size = new TextEncoder().encode(text).length

  return (
    <section aria-labelledby="support-report-title" className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id="support-report-title" className="flex items-center gap-2 text-sm font-medium text-neutral-200">
            <FileText size={14} aria-hidden="true" /> Support report
          </h3>
          <p className="mt-1 max-w-xl text-xs text-neutral-500">
            Versions, service status, which features are configured, and recent errors. Keys, file paths and setting values are removed.
            Read it before you share it.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void prepare()}
          disabled={preparing}
          className="inline-flex items-center gap-1.5 rounded-lg border border-white/[0.08] px-3 py-1.5 text-xs text-neutral-200 hover:bg-white/[0.05] disabled:opacity-50"
        >
          {preparing && <Loader2 size={12} className="animate-spin" aria-hidden="true" />}
          {text ? 'Prepare again' : 'Prepare report'}
        </button>
      </div>

      {error && <p role="alert" className="mt-3 text-xs text-red-300">{error}</p>}

      {text && (
        <div className="mt-3 space-y-2">
          <p className="text-xs text-neutral-400">
            {formatSize(size)}
            {' · '}
            {logIncluded ? 'Includes the recent backend log.' : 'The backend log is left out; turn on support mode to include it.'}
            {truncated && ' Older entries were dropped to keep it small.'}
          </p>
          <pre
            aria-label="Support report contents"
            tabIndex={0}
            className="max-h-80 overflow-auto rounded-lg border border-white/[0.06] bg-black/20 p-3 text-[11px] leading-relaxed text-neutral-300"
          >
            {text}
          </pre>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={save}
              className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90"
            >
              <Download size={12} aria-hidden="true" /> Save report
            </button>
            <button type="button" onClick={() => setText('')} className="rounded-lg px-3 py-1.5 text-xs text-neutral-400 hover:text-neutral-200">
              Discard
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
