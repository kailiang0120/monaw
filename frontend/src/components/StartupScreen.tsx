import { useEffect, useState } from 'react'
import appLogo from '../assets/Logo.png'

/** Cover the incomplete chat UI until startup finishes, with an escape for slow launches. */
export function StartupScreen({ status, progress, onRetry, onContinue }: {
  status: string
  progress: number
  onRetry: () => void
  onContinue: () => void
}) {
  const [slow, setSlow] = useState(false)
  const percent = Math.round(Math.max(0, Math.min(100, progress)))

  useEffect(() => {
    const timer = window.setTimeout(() => setSlow(true), 20_000)
    return () => window.clearTimeout(timer)
  }, [])

  return (
    <main className="flex min-h-0 flex-1 flex-col items-center justify-center gap-5 px-6 pb-12 text-center">
      <img src={appLogo} alt="Monaw" className="h-20 w-20 object-contain" draggable={false} />
      <div className="w-[min(15rem,72vw)]">
        <div
          className="startup-progress"
          role="progressbar"
          aria-label="Loading Monaw"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
        >
          <div className="startup-progress-fill" style={{ width: `${percent}%` }} />
        </div>
        <div className="mt-2.5 flex items-center justify-between gap-3 text-xs text-[var(--st-text-muted)]">
          <p role="status" className="truncate text-left">
            {slow ? 'Startup is taking longer than expected.' : status}
          </p>
          <span className="shrink-0 tabular-nums">{percent}%</span>
        </div>
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
