import {
  AlertCircle,
  CheckCircle2,
  Circle,
  CircleDashed,
  RefreshCw,
} from 'lucide-react'

export interface StepProgress {
  step_id: string
  description: string
  status: 'pending' | 'active' | 'done' | 'failed' | 'retrying'
  observation?: string
  retry_count?: number
}

export interface PlanProgressProps {
  steps: StepProgress[]
  title?: string
}

function StatusIcon({ status }: { status: StepProgress['status'] }) {
  switch (status) {
    case 'pending':
      return <Circle size={14} className="text-neutral-600" />
    case 'active':
      return <CircleDashed size={14} className="animate-spin text-accent-light" />
    case 'done':
      return <CheckCircle2 size={14} className="text-emerald-300" />
    case 'failed':
      return <AlertCircle size={14} className="text-red-300" />
    case 'retrying':
      return <RefreshCw size={14} className="animate-spin text-amber-300" />
    default:
      return <Circle size={14} className="text-neutral-600" />
  }
}

function statusTextClass(status: StepProgress['status']): string {
  switch (status) {
    case 'pending':
      return 'text-neutral-500'
    case 'active':
      return 'text-accent-light'
    case 'done':
      return 'text-emerald-300'
    case 'failed':
      return 'text-red-300'
    case 'retrying':
      return 'text-amber-300'
    default:
      return 'text-neutral-500'
  }
}

export function PlanProgress({
  steps,
  title = 'Plan',
}: PlanProgressProps) {
  const completed = steps.filter((step) => step.status === 'done').length

  return (
    <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-white/[0.025] p-3 text-xs">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="font-medium text-neutral-300">{title}</p>
        <span className="status-pill border-white/[0.08] bg-white/[0.03] text-neutral-500">
          {completed}/{steps.length} done
        </span>
      </div>
      <div className="space-y-2">
        {steps.map((step) => (
          <div key={step.step_id} className="rounded-lg border border-white/[0.06] bg-black/10 px-3 py-2">
            <div className="flex items-start gap-2">
              <span className="mt-0.5 shrink-0">
                <StatusIcon status={step.status} />
              </span>
              <div className="min-w-0 flex-1">
                <p className={`leading-relaxed ${statusTextClass(step.status)}`}>
                  {step.description}
                  {step.retry_count ? (
                    <span className="ml-1 text-amber-400">
                      retry {step.retry_count}
                    </span>
                  ) : null}
                </p>
                {step.observation && (
                  <p className="mt-1 text-[11px] leading-relaxed text-neutral-500">
                    {step.observation}
                  </p>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
