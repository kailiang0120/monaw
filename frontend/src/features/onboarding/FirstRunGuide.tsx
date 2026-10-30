import { AlertCircle, CheckCircle2, CircleDot, Loader2, Play, type LucideIcon } from 'lucide-react'

import type { SettingsPageId } from '../settings/settingsCopy'
import { EXAMPLE_PROMPT, type GuideStep, type StepState } from './readiness'

const STATE_ICON: Record<StepState, { icon: LucideIcon; className: string; label: string }> = {
  ready: { icon: CheckCircle2, className: 'text-accent-light', label: 'Ready' },
  action: { icon: CircleDot, className: 'text-amber-300', label: 'Needs setup' },
  problem: { icon: AlertCircle, className: 'text-red-300', label: 'Problem' },
  checking: { icon: Loader2, className: 'animate-spin text-neutral-500', label: 'Checking' },
}

interface Props {
  required: GuideStep[]
  optional: GuideStep[]
  modelReady: boolean
  /** Running the example also waits for the backend and any active turn. */
  exampleDisabled: boolean
  onOpenSettings: (page: SettingsPageId) => void
  onRunExample: (prompt: string) => void
  onDismiss?: () => void
}

/** First-run checklist: reach one harmless successful task, then show what else is turned on. */
export function FirstRunGuide({ required, optional, modelReady, exampleDisabled, onOpenSettings, onRunExample, onDismiss }: Props) {
  return (
    <section aria-labelledby="first-run-title" className="w-full max-w-md select-text text-left">
      <div className="flex items-center justify-between gap-3">
        <h2 id="first-run-title" className="text-sm font-medium text-neutral-300">Get started</h2>
        {onDismiss && (
          <button type="button" onClick={onDismiss} className="rounded-md px-1.5 py-0.5 text-[11px] text-neutral-500 hover:text-neutral-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30">
            Hide guide
          </button>
        )}
      </div>

      <ol className="mt-2 space-y-1.5">
        {required.map((step) => <StepRow key={step.id} step={step} onOpenSettings={onOpenSettings} />)}
        <li className="flex items-start gap-2.5 rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2">
          <Play size={14} className="mt-0.5 shrink-0 text-neutral-500" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <p className="text-[12px] font-medium text-neutral-300">Try a first task</p>
            <p className="mt-0.5 text-[11px] text-neutral-500">“{EXAMPLE_PROMPT}” It only reads files.</p>
            {!modelReady && <p className="mt-0.5 text-[11px] text-neutral-600">Connect a model first.</p>}
          </div>
          <button
            type="button"
            disabled={!modelReady || exampleDisabled}
            onClick={() => onRunExample(EXAMPLE_PROMPT)}
            className="shrink-0 rounded-md border border-accent/30 px-2 py-1 text-[11px] text-accent-light transition-colors hover:bg-accent/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Run example
          </button>
        </li>
      </ol>

      {optional.length > 0 && (
        <>
          <h3 className="mt-4 text-[11px] font-medium uppercase tracking-[0.12em] text-neutral-600">Turned on</h3>
          <ul className="mt-1.5 space-y-1.5">
            {optional.map((step) => <StepRow key={step.id} step={step} onOpenSettings={onOpenSettings} />)}
          </ul>
        </>
      )}
    </section>
  )
}

function StepRow({ step, onOpenSettings }: { step: GuideStep; onOpenSettings: (page: SettingsPageId) => void }) {
  const { icon: Icon, className, label } = STATE_ICON[step.state]
  return (
    <li className="flex items-start gap-2.5 rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2">
      <Icon size={14} className={`mt-0.5 shrink-0 ${className}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="text-[12px] font-medium text-neutral-300">
          {step.title}
          <span className="sr-only"> ({label})</span>
        </p>
        <p className="mt-0.5 break-words text-[11px] text-neutral-500">{step.detail}</p>
      </div>
      {step.settingsPage && step.actionLabel && (
        <button
          type="button"
          onClick={() => onOpenSettings(step.settingsPage!)}
          aria-label={`${step.actionLabel}: ${step.title}`}
          className="shrink-0 rounded-md border border-white/[0.08] px-2 py-1 text-[11px] text-neutral-300 transition-colors hover:bg-white/[0.05] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
        >
          {step.actionLabel}
        </button>
      )}
    </li>
  )
}
