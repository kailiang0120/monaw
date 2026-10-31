import { AlertTriangle } from 'lucide-react'
import type { ExecutionEnvironment as Environment } from '../lib/api/types'

const HOST_BACKENDS = new Set(['local_direct', 'local_restricted'])

export function runsOnHost(environment?: Environment): boolean {
  if (!environment) return false
  return environment.on_host ?? HOST_BACKENDS.has(environment.backend)
}

function approvalText(environment: Environment): string {
  if (environment.user_review_only) return 'Needs your own approval: Auto Review cannot approve untrusted commands on your PC'
  if (environment.always_ask) return 'Asks you every time: runs on your PC instead of Docker'
  return environment.approval_required
    ? 'Explicit approval required by your permission mode'
    : 'Approval follows your permission mode; no explicit confirmation required'
}

export function ExecutionEnvironment({ environment }: { environment?: Environment }) {
  if (!environment) return null
  const onHost = runsOnHost(environment)
  return (
    <div
      className={`my-2 space-y-1 rounded-lg border p-2 text-[11px] leading-relaxed text-neutral-300 ${onHost ? 'border-amber-400/30 bg-amber-400/[0.06]' : 'border-white/10'}`}
      aria-label="Execution environment"
    >
      <p className={`flex items-start gap-1.5 font-semibold ${onHost ? 'text-amber-200' : ''}`}>
        {onHost && <AlertTriangle size={12} className="mt-0.5 shrink-0 text-amber-300" aria-hidden />}
        <span>{environment.summary}</span>
      </p>
      <p>Backend: {environment.backend}{environment.shell ? ` · ${environment.shell}` : ''}</p>
      {environment.image && <p className="break-all">Image: {environment.image}</p>}
      {environment.workspace && <p className="break-all">Workspace: {environment.workspace}</p>}
      {environment.execution_workspace !== environment.workspace && <p>Runs in: {environment.execution_workspace}</p>}
      <p>{environment.filesystem}</p>
      <p>{environment.network}</p>
      <p>{environment.writes}</p>
      <p className={environment.always_ask || environment.user_review_only ? 'text-amber-200' : ''}>{approvalText(environment)}</p>
      {environment.reason && <p>{environment.reason}</p>}
      <p className="text-neutral-400">{environment.scope}</p>
    </div>
  )
}
