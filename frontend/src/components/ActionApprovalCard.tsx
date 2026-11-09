import type { ActionDescriptor } from '../lib/api/types'

export function ActionApprovalCard({ action }: { action?: ActionDescriptor }) {
  if (!action?.action_id) return null
  const scope = action.data_scope
  const target = action.target
  const destination = action.destination || target?.origin || target?.app || target?.server_id
  const effects: Record<string, string> = {
    none: 'Reads information', local_mutation: 'Changes local state',
    external_mutation: 'May send data or change the remote service',
    irreversible: 'May permanently change or remove data',
    unknown: 'Effects cannot be fully inspected before execution',
  }
  return (
    <div className="my-3 space-y-2 rounded-lg border border-amber-400/20 p-3 text-xs text-neutral-200">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2">
        <dt className="text-neutral-400">Action</dt>
        <dd className="break-words">{action.action_kind.replace(/_/g, ' ')}</dd>
        <dt className="text-neutral-400">Destination</dt>
        <dd className="break-all">{destination || 'Not known'}</dd>
        {!!action.also_sends_to?.length && <><dt className="text-neutral-400">Also sends to</dt><dd className="break-all">{action.also_sends_to.join(', ')}</dd></>}
        {target?.page_id !== undefined && <><dt className="text-neutral-400">Tab</dt><dd>{target.page_id}</dd></>}
        {action.side_effect && <><dt className="text-neutral-400">Effect</dt><dd className="break-words">{effects[action.side_effect] || action.side_effect.replace(/_/g, ' ')}</dd></>}
        {!!scope?.file_count && <><dt className="text-neutral-400">Files</dt><dd>{scope.file_count} selected{scope.size_bytes !== undefined ? ` · ${scope.size_bytes.toLocaleString()} bytes` : ''}</dd></>}
      </dl>
      {!!scope?.files?.length && <ul className="space-y-1 break-all" aria-label="Selected files">{scope.files.map((file, i) => <li key={i}>{file.name || file.path?.split(/[\\/]/).pop() || 'Selected file'}{file.size_bytes !== undefined ? ` (${file.size_bytes.toLocaleString()} bytes)` : ''}</li>)}</ul>}
      {action.opaque
        ? <p className="text-amber-200">Monaw cannot see everything this action does. Approving lets it run once; it does not limit what it sends or where.</p>
        : <p className="text-neutral-400">
            Approval applies once to this action and destination. Changed details require a new check.
            {action.side_effect === 'external_mutation' && action.target?.page_id !== undefined
              && ' Scripts on the page can still change what is sent after you approve.'}
          </p>}
      {action.human_only && <p className="text-amber-200">This action needs your decision. Its external effects may not be reversible.</p>}
    </div>
  )
}
