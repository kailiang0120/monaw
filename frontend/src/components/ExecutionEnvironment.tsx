import type { ExecutionEnvironment as Environment } from '../lib/api/types'

export function ExecutionEnvironment({ environment }: { environment?: Environment }) {
  if (!environment) return null
  return (
    <div className="my-2 space-y-1 rounded-lg border border-white/10 p-2 text-[11px] leading-relaxed text-neutral-300" aria-label="Execution environment">
      <p className="font-semibold">{environment.summary}</p>
      <p>Backend: {environment.backend}{environment.shell ? ` · ${environment.shell}` : ''}</p>
      {environment.image && <p className="break-all">Image: {environment.image}</p>}
      {environment.workspace && <p className="break-all">Workspace: {environment.workspace}</p>}
      {environment.execution_workspace !== environment.workspace && <p>Runs in: {environment.execution_workspace}</p>}
      <p>{environment.filesystem}</p>
      <p>{environment.network}</p>
      <p>{environment.writes}</p>
      <p>{environment.approval_required ? 'Explicit approval required by your permission mode' : 'Approval follows your permission mode; no explicit confirmation required'}</p>
      {environment.reason && <p>{environment.reason}</p>}
      <p className="text-neutral-400">{environment.scope}</p>
    </div>
  )
}
