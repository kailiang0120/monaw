import { useEffect, useState } from 'react'
import type { ActionDescriptor } from '../lib/api/types'
import { fetchApprovalPreview } from '../lib/api/approvals'

/** Show public action facts and a separate authenticated preview while approval is pending. */
export function ActionApprovalCard({
  action,
  ticketId,
  onPreviewReady,
}: {
  action?: ActionDescriptor
  ticketId?: string
  onPreviewReady?: (ready: boolean) => void
}) {
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    setPreview(null)
    setError('')
    onPreviewReady?.(false)
    if (ticketId && action?.preview_required) {
      void fetchApprovalPreview(ticketId)
        .then((result) => {
          if (!active) return
          if (result.authorization_binding !== action.authorization_binding)
            throw new Error('Changed action')
          setPreview(result.parameters)
          onPreviewReady?.(true)
        })
        .catch(() => {
          if (active)
            setError(
              'Action details are unavailable. Reject this request and ask the agent to try again.',
            )
        })
    }
    return () => {
      active = false
    }
  }, [ticketId, action?.preview_required, action?.authorization_binding, onPreviewReady])
  if (!action?.action_id) return null
  const scope = action.data_scope
  const target = action.target
  const destination = action.destination || target?.origin || target?.app || target?.server_id
  const effects: Record<string, string> = {
    none: 'Reads information',
    local_mutation: 'Changes local state',
    external_mutation: 'May send data or change the remote service',
    irreversible: 'May permanently change or remove data',
    unknown: 'Effects cannot be fully inspected before execution',
  }
  return (
    <div className="my-3 space-y-2 rounded-lg border border-[var(--st-warn-border)] p-3 text-xs text-[var(--st-text)]">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2">
        <dt className="text-[var(--st-text-muted)]">Action</dt>
        <dd className="break-words">{action.action_kind.replace(/_/g, ' ')}</dd>
        <dt className="text-[var(--st-text-muted)]">Destination</dt>
        <dd className="break-all">{destination || 'Not known'}</dd>
        {!!action.also_sends_to?.length && (
          <>
            <dt className="text-[var(--st-text-muted)]">Also sends to</dt>
            <dd className="break-all">{action.also_sends_to.join(', ')}</dd>
          </>
        )}
        {target?.page_id !== undefined && (
          <>
            <dt className="text-[var(--st-text-muted)]">Tab</dt>
            <dd>{target.page_id}</dd>
          </>
        )}
        {action.side_effect && (
          <>
            <dt className="text-[var(--st-text-muted)]">Effect</dt>
            <dd className="break-words">
              {effects[action.side_effect] || action.side_effect.replace(/_/g, ' ')}
            </dd>
          </>
        )}
        {!!scope?.file_count && (
          <>
            <dt className="text-[var(--st-text-muted)]">Files</dt>
            <dd>
              {scope.file_count} selected
              {scope.size_bytes !== undefined
                ? ` · ${scope.size_bytes.toLocaleString()} bytes`
                : ''}
            </dd>
          </>
        )}
      </dl>
      {ticketId && action.preview_required && (
        <section aria-label="Action details" className="space-y-2">
          {preview ? (
            <>
              <dl className="max-h-64 space-y-2 overflow-auto rounded-lg bg-[var(--st-surface-sunken)] p-3">
                {Object.entries(preview).map(([key, value]) => (
                  <div key={key}>
                    <dt className="st-label capitalize">{key.replace(/_/g, ' ')}</dt>
                    <dd>
                      <pre className="whitespace-pre-wrap break-all font-mono text-xs">
                        {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
                      </pre>
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="st-hint">Known secrets and environment values are hidden.</p>
            </>
          ) : (
            <p role={error ? 'alert' : 'status'} className="st-desc">
              {error || 'Loading action details…'}
            </p>
          )}
        </section>
      )}
      {!!Object.keys(action.connector_preview || {}).length && (
        <dl
          aria-label="GitHub action details"
          className="max-h-64 space-y-2 overflow-auto rounded-lg bg-[var(--st-surface-sunken)] p-3 text-[var(--st-text)]"
        >
          {Object.entries(action.connector_preview || {}).map(([key, value]) => (
            <div key={key}>
              <dt className="font-medium capitalize">{key.replace(/_/g, ' ')}</dt>
              <dd className="whitespace-pre-wrap break-words">{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
      {!!scope?.files?.length && (
        <ul className="space-y-1 break-all" aria-label="Selected files">
          {scope.files.map((file, i) => (
            <li key={i}>
              {file.name || file.path?.split(/[\\/]/).pop() || 'Selected file'}
              {file.size_bytes !== undefined ? ` (${file.size_bytes.toLocaleString()} bytes)` : ''}
            </li>
          ))}
        </ul>
      )}
      {action.opaque ? (
        <p className="text-[var(--st-warn)]">
          Monaw cannot see everything this action does. Approving lets it run once; it does not
          limit what it sends or where.
        </p>
      ) : (
        <p className="text-[var(--st-text-muted)]">
          Approval applies once to this action and destination. Changed details require a new check.
          {action.side_effect === 'external_mutation' &&
            action.target?.page_id !== undefined &&
            ' Scripts on the page can still change what is sent after you approve.'}
        </p>
      )}
      {action.human_only && (
        <p className="text-[var(--st-warn)]">
          This action needs your decision. Its external effects may not be reversible.
        </p>
      )}
    </div>
  )
}
