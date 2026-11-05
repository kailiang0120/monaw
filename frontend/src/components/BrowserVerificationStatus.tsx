import type { BrowserActionOutcome } from '../lib/browserAction'

export function BrowserVerificationStatus({ outcome, detail = false }: { outcome?: BrowserActionOutcome; detail?: boolean }) {
  if (!outcome) return null
  const labels = { executed: 'Dispatched · not verified', verified: 'Verified', failed: 'Verification failed', uncertain: 'Outcome uncertain', needs_review: 'Needs review' }
  return <span className={`text-xs ${outcome.status === 'verified' ? 'text-emerald-300' : 'text-amber-300'}`}>
    {labels[outcome.status]}
    {detail && outcome.status !== 'verified' && <span className="mt-1 block text-neutral-400">{outcome.status === 'executed' ? 'The command was dispatched. The intended page change has not been confirmed.' : 'Inspect the current page before retrying. The action may already have taken effect.'}</span>}
    {detail && outcome.recovery?.reason_code && <span className="mt-1 block text-neutral-400">{outcome.recovery.reason_code.replace(/_/g, ' ')}</span>}
  </span>
}
