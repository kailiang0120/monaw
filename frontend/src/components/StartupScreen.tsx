import { useEffect, useState } from 'react'
import appLogo from '../assets/Logo.png'

/** Cover the incomplete chat UI until startup finishes, with an escape for slow launches. */
export function StartupScreen({ status, onRetry, onContinue }: {
  status: string
  onRetry: () => void
  onContinue: () => void
}) {
  const [slow, setSlow] = useState(false)

  useEffect(() => {
    const timer = window.setTimeout(() => setSlow(true), 20_000)
    return () => window.clearTimeout(timer)
  }, [])

  return (
    <main className="flex min-h-0 flex-1 flex-col items-center justify-center gap-5 px-6 pb-12 text-center">
      <img src={appLogo} alt="Monaw" className="h-20 w-20 object-contain" draggable={false} />
      <div className="flex items-center gap-2.5 text-sm text-[var(--st-text-muted)]">
        <progress className="startup-spinner" aria-label="Loading Monaw" />
        <p role="status">{slow ? 'Startup is taking longer than expected.' : status}</p>
      </div>
      {slow && (
        <div className="flex flex-wrap justify-center gap-3">
          <button type="button" className="control rounded-lg px-4 py-2 text-sm" onClick={onRetry}>Try again</button>
          <button type="button" className="rounded-lg px-4 py-2 text-sm text-[var(--st-text-muted)] hover:text-[var(--st-text)]" onClick={onContinue}>Open app</button>
        </div>
      )}
    </main>
  )
}
