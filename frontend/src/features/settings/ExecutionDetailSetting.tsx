import { ChoiceCard, SettingsCard } from '../../components/ui/Panel'
import type { AgentSettings } from '../../lib/api/types'

type ExecutionDetail = AgentSettings['execution_detail']

const EXECUTION_DETAIL_CHOICES: Array<{
  value: ExecutionDetail
  title: string
  summary: string
  description: string
}> = [
  {
    value: 'simple',
    title: 'Concise',
    summary: 'Keep the chat focused.',
    description: 'Show tool activity and essential controls without an execution diagnostics panel.',
  },
  {
    value: 'detailed',
    title: 'Detailed',
    summary: 'Inspect timing and operations.',
    description: 'Adds an expandable Execution details section below the conversation.',
  },
  {
    value: 'developer',
    title: 'Developer',
    summary: 'Troubleshoot a run.',
    description: 'Includes Detailed mode plus technical IDs, event history and operation metadata inside the same section.',
  },
]

export function ExecutionDetailSetting({
  value,
  onChange,
}: {
  value: ExecutionDetail
  onChange: (value: ExecutionDetail) => void
}) {
  return (
    <SettingsCard
      title="Execution detail"
      description="Choose how much information appears in execution cards."
      footnote="This changes display only. It does not change permissions or how a Run executes."
    >
      <div className="grid gap-2 p-4 sm:grid-cols-3" role="group" aria-label="Execution detail mode">
        {EXECUTION_DETAIL_CHOICES.map((choice) => (
          <ChoiceCard
            key={choice.value}
            ariaLabel={choice.title}
            title={choice.title}
            summary={choice.summary}
            description={choice.description}
            selected={value === choice.value}
            onSelect={() => onChange(choice.value)}
          />
        ))}
      </div>
    </SettingsCard>
  )
}
