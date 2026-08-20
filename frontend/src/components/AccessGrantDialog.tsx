import { useState } from 'react'
import { CheckCircle2, Clock, Infinity, Shield, ShieldCheck, ShieldX } from 'lucide-react'
import { resolveAccessGrant } from '../lib/api/accessGrants'
import type { AccessGrantDecision } from '../lib/api/types'
import type { AccessGrantNotice } from '../hooks/useChat'

interface Props {
  ticket: AccessGrantNotice
  onResolved: () => void
}

export function AccessGrantDialog({ ticket, onResolved }: Props) {
  const [resolving, setResolving] = useState(false)

  const handleDecision = async (decision: AccessGrantDecision) => {
    setResolving(true)
    try {
      await resolveAccessGrant(ticket.ticket_id, decision)
    } catch {
      // The stream will keep waiting or time out if the backend did not accept it.
    } finally {
      setResolving(false)
      onResolved()
    }
  }

  const typeLabel = ticket.target_type === 'app' ? 'Application' : 'Path'
  const TypeIcon = ticket.target_type === 'app' ? Shield : ShieldCheck

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
      <div className="panel w-full max-w-md overflow-hidden rounded-2xl">
        <div className="flex items-center gap-3 border-b border-white/[0.07] px-5 py-4">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-amber-400/20 bg-amber-400/10">
            <TypeIcon size={20} className="text-amber-300" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-neutral-100">Access required</h2>
            <p className="text-xs text-neutral-500">{typeLabel} permission</p>
          </div>
        </div>

        <div className="space-y-3 px-5 py-4">
          <div className="rounded-xl border border-white/[0.08] bg-white/[0.035] px-4 py-3">
            <p className="mb-1 text-xs text-neutral-500">Requested access</p>
            <p className="break-all text-sm font-medium text-neutral-100">
              {ticket.requested_access ? `${ticket.requested_access} · ` : ''}{ticket.display_name}
            </p>
            {ticket.target_identifier !== ticket.display_name && (
              <p className="mt-1 break-all text-xs text-neutral-500">
                {ticket.target_identifier}
              </p>
            )}
          </div>

          {ticket.action_context && (
            <div className="rounded-xl border border-white/[0.06] bg-black/15 px-4 py-3">
              <p className="mb-1 text-xs text-neutral-500">Reason</p>
              <p className="text-sm leading-relaxed text-neutral-300">{ticket.action_context}</p>
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2 border-t border-white/[0.07] px-5 py-4">
          <DecisionButton
            icon={Clock}
            label="Allow Once"
            disabled={resolving}
            onClick={() => handleDecision('once')}
          />
          <DecisionButton
            icon={CheckCircle2}
            label="This Session"
            disabled={resolving}
            onClick={() => handleDecision('session')}
          />
          <DecisionButton
            icon={Infinity}
            label="Always Allow"
            disabled={resolving}
            onClick={() => handleDecision('always')}
            tone="emerald"
          />
          <DecisionButton
            icon={ShieldX}
            label="Deny"
            disabled={resolving}
            onClick={() => handleDecision('deny')}
            tone="red"
          />
        </div>
      </div>
    </div>
  )
}

function DecisionButton({
  icon: Icon,
  label,
  disabled,
  onClick,
  tone = 'neutral',
}: {
  icon: typeof Clock
  label: string
  disabled: boolean
  onClick: () => void
  tone?: 'neutral' | 'emerald' | 'red'
}) {
  const toneClass = {
    neutral: 'border-white/[0.08] bg-white/[0.035] text-neutral-200 hover:bg-white/[0.06]',
    emerald: 'border-emerald-400/25 bg-emerald-400/10 text-emerald-300 hover:bg-emerald-400/15',
    red: 'border-red-400/25 bg-red-400/10 text-red-300 hover:bg-red-400/15',
  }[tone]

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`flex items-center justify-center gap-2 rounded-xl border px-4 py-2.5 text-sm font-medium transition-colors disabled:opacity-50 ${toneClass}`}
    >
      <Icon size={14} />
      {label}
    </button>
  )
}
