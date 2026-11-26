import { useId } from 'react'
import { Brain, Loader2 } from 'lucide-react'
import type { ModelOptions } from '../lib/api/types'
import {
  modelLabel,
  modelSelectionKey,
  modelUnavailableReason,
  reasoningEffortsForProvider,
  selectModel,
  type ModelSelection,
} from '../lib/modelSelection'
import { Dropdown, type DropdownOption } from './Dropdown'

interface Props {
  value: ModelSelection
  catalog: ModelOptions
  onChange: (selection: ModelSelection) => void
  disabled?: boolean
  loading?: boolean
  onRetry?: () => void
  label?: string
  compact?: boolean
  followDefault?: boolean
  onFollowDefault?: () => void
}

/** Share the global dropdown, catalog labels, and capabilities across screens. */
export function ModelPicker({
  value,
  catalog,
  onChange,
  disabled,
  loading = false,
  onRetry,
  label = 'Model',
  compact = false,
  followDefault = false,
  onFollowDefault,
}: Props) {
  const id = useId()
  const selectedKey = modelSelectionKey(value)
  const choices = catalog.providers.flatMap((connection) => connection.models.map((model_name) => {
    const selection = { provider: connection.id, model_name }
    return {
      selection,
      value: modelSelectionKey(selection),
      label: modelLabel(selection, catalog),
      group: connection.label,
      disabled: connection.available === false,
    }
  }))
  const selectedListed = choices.some((choice) => choice.value === selectedKey)
  const selectedConnection = catalog.providers.find((connection) => connection.id === value.provider)
  const missingLabel = !selectedConnection
    ? 'catalog unavailable'
    : selectedConnection.available === false ? 'unavailable' : 'not listed'
  const options: DropdownOption<string>[] = choices.map((choice) => ({
    ...choice,
    label: `${choice.label}${choice.disabled ? ' · unavailable' : ''}`,
    shortLabel: choice.label,
  }))
  if (!selectedListed) {
    options.unshift({
      value: selectedKey,
      label: `${selectedConnection ? modelLabel(value, catalog) : value.model_name} · ${missingLabel}`,
      shortLabel: modelLabel(value, catalog),
      disabled: true,
    })
  }
  if (onFollowDefault) {
    options.unshift({ value: 'default', label: 'Follow the default model', alwaysVisible: true })
  }
  const efforts = reasoningEffortsForProvider(value.provider, value.model_name, catalog)
  const reason = loading || followDefault ? '' : modelUnavailableReason(value, catalog)
  const displayValue = followDefault ? 'default' : selectedKey
  const displayOptions = loading ? [{ value: displayValue, label: 'Loading models…' }] : options
  const title = loading ? 'Loading models…'
    : followDefault ? 'Follow the default model'
      : selectedConnection ? modelLabel(value, catalog) : value.model_name
  const effortOptions: DropdownOption<ModelSelection['reasoning_effort']>[] = efforts.map((effort) => ({
    value: effort,
    label: effort === 'xhigh' ? 'Extra high' : effort[0].toUpperCase() + effort.slice(1),
  }))
  if (!efforts.includes(value.reasoning_effort)) {
    effortOptions.unshift({ value: value.reasoning_effort, label: `${value.reasoning_effort} · unavailable`, disabled: true })
  }

  /** Apply explicit model changes while preserving unsupported saved values. */
  const chooseModel = (key: string) => {
    if (key === 'default') {
      onFollowDefault?.()
      return
    }
    const choice = choices.find((entry) => entry.value === key)
    if (choice && !choice.disabled) {
      onChange(selectModel(value, choice.selection.provider, choice.selection.model_name, catalog))
    }
  }

  return (
    <div aria-busy={loading || undefined} className={`min-w-0 ${compact ? 'flex flex-wrap items-center justify-end gap-2' : 'space-y-2'}`}>
      <div className={compact ? 'min-w-0 max-w-[42vw]' : ''}>
        <label className={compact ? 'sr-only' : 'st-label mb-1 block'} htmlFor={`${id}-model`}>{label}</label>
        <Dropdown
          id={`${id}-model`}
          ariaLabel={label}
          ariaDescribedBy={reason ? `${id}-status` : undefined}
          value={displayValue}
          options={displayOptions}
          onChange={chooseModel}
          disabled={disabled || loading}
          searchable={!loading && choices.length > 10}
          searchLabel="Search models"
          size={compact ? 'sm' : 'md'}
          align={compact ? 'right' : 'left'}
          className={compact ? 'composer-model-control' : ''}
          icon={compact ? loading
            ? <Loader2 size={12} className="composer-model-icon animate-spin motion-reduce:animate-none" aria-hidden="true" />
            : <Brain size={12} className="composer-model-icon" aria-hidden="true" /> : undefined}
          title={`${label}: ${title}`}
        />
      </div>
      {!loading && !followDefault && efforts.length > 0 && (
        <div className={compact ? 'w-[105px]' : ''}>
          <label className={compact ? 'sr-only' : 'st-label mb-1 block'} htmlFor={`${id}-effort`}>Reasoning effort</label>
          <Dropdown
            id={`${id}-effort`}
            ariaLabel="Reasoning effort"
            value={value.reasoning_effort}
            options={effortOptions}
            disabled={disabled || !!reason}
            size={compact ? 'sm' : 'md'}
            align={compact ? 'right' : 'left'}
            onChange={(reasoning_effort) => onChange({ ...value, reasoning_effort })}
          />
        </div>
      )}
      {reason && (
        <div className="model-picker-status">
          <span id={`${id}-status`} role="status">{reason}</span>
          {!selectedConnection && onRetry && (
            <button type="button" className="model-picker-retry" onClick={onRetry}>Retry</button>
          )}
        </div>
      )}
    </div>
  )
}
