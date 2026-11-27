import { useState } from 'react'
import { Download, FileText, Loader2 } from 'lucide-react'

import { Note } from '../../components/ui/Panel'
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
    <section aria-labelledby="support-report-title" className="st-card">
      <header className="st-card-head flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id="support-report-title" className="st-label flex items-center gap-2">
            <FileText size={14} aria-hidden="true" /> Support report
          </h3>
          <p className="st-desc mt-1">
            Versions, service status, which features are configured, and recent errors. Keys, file paths and setting values are removed.
            Read it before you share it.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void prepare()}
          disabled={preparing}
          className="st-btn st-btn-secondary shrink-0"
        >
          {preparing && <Loader2 size={12} className="animate-spin" aria-hidden="true" />}
          {text ? 'Prepare again' : 'Prepare report'}
        </button>
      </header>

      {error && (
        <div role="alert" className="p-4">
          <Note tone="danger">{error}</Note>
        </div>
      )}

      {text && (
        <div className="space-y-3 p-4">
          <p className="st-desc">
            {formatSize(size)}
            {' · '}
            {logIncluded ? 'Includes the recent backend log.' : 'The backend log is left out; turn on support mode to include it.'}
            {truncated && ' Older entries were dropped to keep it small.'}
          </p>
          <pre
            aria-label="Support report contents"
            tabIndex={0}
            className="st-code max-h-80 overflow-auto whitespace-pre-wrap break-words"
          >
            {text}
          </pre>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={save}
              className="st-btn st-btn-primary"
            >
              <Download size={12} aria-hidden="true" /> Save report
            </button>
            <button type="button" onClick={() => setText('')} className="st-btn st-btn-ghost">
              Discard
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
