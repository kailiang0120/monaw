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
    summary: 'Status and controls at a glance.',
    description: 'Keeps the execution card compact. You can still expand operation details whenever you need them.',
  },
  {
    value: 'detailed',
    title: 'Detailed',
    summary: 'Show Run timing and operation summaries.',
    description: 'Adds timing and operation context from the selected Run. Operation details can still be collapsed temporarily.',
  },
  {
    value: 'developer',
    title: 'Developer',
    summary: 'Show Run identifiers and diagnostic metadata.',
    description: 'Adds the selected Run ID, revision and event cursor, plus matching operation evidence and event metadata.',
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
