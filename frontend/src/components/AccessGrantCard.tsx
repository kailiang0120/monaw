import { useState } from 'react'
import { CheckCircle2, Clock, Infinity, Shield, ShieldCheck, ShieldX } from 'lucide-react'
import { resolveAccessGrant } from '../lib/api/accessGrants'
import { ApiError } from '../lib/api/client'
import type { AccessGrantDecision } from '../lib/api/types'
import type { AccessGrantNotice } from '../hooks/useChat'

interface Props {
  ticket: AccessGrantNotice
  onResolved: () => void
}

export function AccessGrantCard({ ticket, onResolved }: Props) {
  const [resolving, setResolving] = useState(false)
  const [error, setError] = useState('')

  const handleDecision = async (decision: AccessGrantDecision) => {
    setResolving(true)
    setError('')
    try {
      await resolveAccessGrant(ticket.ticket_id, decision)
      onResolved()
    } catch (resolveError) {
      if (resolveError instanceof ApiError && resolveError.status === 404) {
        // Already answered, expired, or replaced: move on to whatever is still pending.
        onResolved()
        return
      }
      // Keep the card so the user can retry; closing it would leave the run waiting silently.
      setError('Could not send your decision. Try again or dismiss.')
    } finally {
      setResolving(false)
    }
  }

  const typeLabel = ticket.target_type === 'app' ? 'Application' : 'Path'
  const TypeIcon = ticket.target_type === 'app' ? Shield : ShieldCheck
  const requested = `${ticket.requested_access ? `${ticket.requested_access} · ` : ''}${ticket.display_name}`
  const detail = [
    ticket.target_identifier !== ticket.display_name ? ticket.target_identifier : '',
    ticket.action_context,
  ].filter(Boolean).join(' — ')

  return (
    <div className="bg-[#11100f] px-4 pt-2">
      <section
        aria-label="Access required"
        className="panel mx-auto flex max-w-3xl animate-slide-up flex-wrap items-center gap-x-3 gap-y-2 rounded-xl px-3 py-2"
      >
        <div className="flex min-w-0 flex-1 basis-64 items-start gap-2.5">
          <TypeIcon size={15} className="mt-0.5 shrink-0 text-amber-300" aria-hidden />
          <div className="min-w-0 text-xs">
            <p className="flex min-w-0 items-baseline gap-1.5">
              <span className="shrink-0 font-medium text-amber-200">{typeLabel} access</span>
              <span className="truncate font-mono text-neutral-200" title={requested}>{requested}</span>
            </p>
            {detail && (
              <p className="mt-0.5 truncate text-[11px] text-neutral-500" title={detail}>{detail}</p>
            )}
            {error && (
              <p className="mt-0.5 text-[11px] text-red-300" role="alert">
                {error}{' '}
                <button type="button" onClick={onResolved} className="underline hover:text-red-200">
                  Dismiss
                </button>
              </p>
            )}
          </div>
        </div>

        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <DecisionButton
            icon={Clock}
            label="Allow once"
            disabled={resolving}
            onClick={() => handleDecision('once')}
          />
          <DecisionButton
            icon={CheckCircle2}
            label="This session"
            disabled={resolving}
            onClick={() => handleDecision('session')}
          />
          <DecisionButton
            icon={Infinity}
            label="Always"
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
      </section>
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
      className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:opacity-50 ${toneClass}`}
    >
      <Icon size={12} aria-hidden />
      {label}
    </button>
  )
}
