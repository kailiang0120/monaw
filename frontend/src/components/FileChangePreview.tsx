import { FileText, Loader2, ShieldCheck } from 'lucide-react'
import { UnifiedDiff } from './UnifiedDiff'

export interface FileChangePreviewFile {
  path: string
  role?: string
  before?: Record<string, unknown>
  after?: Record<string, unknown>
}

/** A bounded, sanitized preview returned by the file mutation planner. */
export interface FileChangePreviewData {
  mutation_id?: string
  revision?: number
  operation?: string
  files: FileChangePreviewFile[]
  preview: Record<string, unknown>
  undo_capability?: string
  apply_available?: boolean
  reason_code?: string | null
  expires_in_seconds?: number
}

export interface FileChangePreviewProps {
  preview: FileChangePreviewData
  isApplying?: boolean
  applied?: boolean
  applyError?: string
  onApply?: () => void | Promise<void>
}

function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/^\w/, (letter) => letter.toUpperCase())
}

function fileStateText(state?: Record<string, unknown>): string {
  if (!state) return 'State unavailable'
  if (state.exists === false) return 'Does not exist'
  const kind = typeof state.kind === 'string' ? state.kind : 'file'
  const size = typeof state.size === 'number' && Number.isFinite(state.size)
    ? ` · ${state.size.toLocaleString()} bytes`
    : ''
  return `${humanize(kind)}${size}`
}

function previewReasonText(reason: string): string {
  if (reason === 'binary_or_undecodable') return 'Text diff is unavailable for this binary or encoded file.'
  if (reason === 'input_limit' || reason === 'line_limit' || reason === 'output_limit') {
    return 'The file is too large to show a text diff. File size changes are shown instead.'
  }
  return 'A text diff is unavailable. File size changes are shown instead.'
}

function byteSummary(label: string, value: unknown): string | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return undefined
  return `${label}: ${value.toLocaleString()} bytes`
}

export function FileChangePreview({
  preview,
  isApplying = false,
  applied = false,
  applyError,
  onApply,
}: FileChangePreviewProps) {
  const diff = preview.preview
  const kind = typeof diff.kind === 'string' ? diff.kind : 'summary'
  const diffText = typeof diff.text === 'string' ? diff.text : ''
  const reason = typeof diff.reason === 'string' ? diff.reason : ''
  const truncated = diff.truncated === true
  const canApply = Boolean(preview.mutation_id && preview.apply_available === true && onApply && !applied)

  return (
    <section aria-label="File change preview" className="overflow-hidden rounded-lg border border-white/[0.08] bg-black/20">
      <header className="flex items-center gap-2 border-b border-white/[0.06] px-3 py-2">
        <FileText size={14} className="shrink-0 text-accent-light" aria-hidden="true" />
        <h3 className="min-w-0 flex-1 text-[11px] font-medium text-neutral-200">File change preview</h3>
        {preview.operation && <span className="text-[10px] text-neutral-500">{humanize(preview.operation)}</span>}
      </header>

      <div className="space-y-2.5 p-3">
        <ul className="space-y-2" aria-label="Affected files">
          {preview.files.map((file, index) => (
            <li key={`${file.path}-${index}`} className="rounded-md border border-white/[0.05] bg-white/[0.02] px-2.5 py-2">
              <p className="break-all font-mono text-[10px] leading-relaxed text-neutral-300">{file.path}</p>
              {file.role && <p className="mt-1 text-[10px] text-neutral-500">{humanize(file.role)}</p>}
              <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-[10px]">
                <dt className="text-neutral-600">Before</dt>
                <dd className="text-neutral-400">{fileStateText(file.before)}</dd>
                <dt className="text-neutral-600">After</dt>
                <dd className="text-neutral-400">{fileStateText(file.after)}</dd>
              </dl>
            </li>
          ))}
        </ul>

        {kind === 'unified_diff' && diffText ? (
          <div>
            <p className="mb-1 text-[10px] font-medium text-neutral-500">Text diff</p>
            <UnifiedDiff label="Text diff" text={diffText} className="max-h-72" />
          </div>
        ) : (
          <div className="rounded-md bg-white/[0.025] px-2.5 py-2">
            <p className="text-[10px] leading-relaxed text-neutral-400">{previewReasonText(reason)}</p>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-neutral-500">
              {byteSummary('Before', diff.before_bytes) && <span>{byteSummary('Before', diff.before_bytes)}</span>}
              {byteSummary('After', diff.after_bytes) && <span>{byteSummary('After', diff.after_bytes)}</span>}
            </div>
          </div>
        )}

        {truncated && <p className="text-[10px] text-amber-200" role="status">The diff was shortened to fit this preview.</p>}
        {preview.undo_capability && (
          <p className="flex items-start gap-1.5 text-[10px] leading-relaxed text-neutral-500">
            <ShieldCheck size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
            Expected Undo support: {humanize(preview.undo_capability)}. Availability is checked again after the file change.
          </p>
        )}
        {preview.reason_code && preview.apply_available === false && (
          <p className="text-[10px] text-amber-200" role="status">Apply unavailable: {humanize(preview.reason_code)}</p>
        )}
        {preview.expires_in_seconds !== undefined && (
          <p className="text-[10px] text-neutral-600">This preview expires in {Math.max(0, Math.ceil(preview.expires_in_seconds / 60))} minutes.</p>
        )}
        {applyError && <p role="alert" className="text-[10px] text-red-200">{applyError}</p>}

        {canApply && (
          <button
            type="button"
            disabled={isApplying}
            onClick={() => void onApply?.()}
            className="inline-flex items-center gap-1.5 rounded-md border border-accent/30 bg-accent/10 px-2.5 py-1.5 text-[10px] font-medium text-accent-light hover:bg-accent/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isApplying && <Loader2 size={12} className="animate-spin" aria-hidden="true" />}
            {isApplying ? 'Applying…' : 'Apply file change'}
          </button>
        )}
        {applied && <p className="text-[10px] text-emerald-300" role="status">File change applied.</p>}
      </div>
    </section>
  )
}
