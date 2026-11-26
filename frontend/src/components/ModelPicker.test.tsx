import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ModelPicker } from './ModelPicker'
import type { ModelOptions } from '../lib/api/types'
import { normalizeDraft } from '../features/settings/settingsConfig'
import type { AgentSettings } from '../lib/api/types'

afterEach(cleanup)

it('keeps an unavailable selection unchanged and uses catalog reasoning capabilities when switching', () => {
  const onChange = vi.fn()
  const catalog: ModelOptions = { providers: [{
    id: 'codex', label: 'Account', available: true, models: ['model:one', 'model:two'],
    labels: { 'model:one': 'First model', 'model:two': 'Second model' },
    reasoning_efforts: { 'model:one': ['low', 'high'], 'model:two': [] },
  }] }
  const initial = { provider: 'codex' as const, model_name: 'removed:model', reasoning_effort: 'medium' as const }
  const { rerender } = render(<ModelPicker value={initial} catalog={catalog} onChange={onChange} />)
  expect(screen.getByRole('button', { name: 'Model' })).toHaveTextContent('removed:model · Account')
  fireEvent.click(screen.getByRole('button', { name: 'Model' }))
  expect(screen.getByRole('option', { name: 'removed:model · Account · not listed' })).toBeInTheDocument()
  expect(onChange).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('option', { name: 'First model · Account' }))
  expect(onChange).toHaveBeenCalledWith({ provider: 'codex', model_name: 'model:one', reasoning_effort: 'low' })
  rerender(<ModelPicker value={{ ...initial, model_name: 'model:one', reasoning_effort: 'low' }} catalog={catalog} onChange={onChange} />)
  fireEvent.click(screen.getByRole('button', { name: 'Reasoning effort' }))
  fireEvent.click(screen.getByRole('option', { name: 'High' }))
  expect(onChange).toHaveBeenLastCalledWith({ provider: 'codex', model_name: 'model:one', reasoning_effort: 'high' })
  rerender(<ModelPicker value={{ ...initial, model_name: 'model:two' }} catalog={catalog} onChange={onChange} />)
  expect(screen.queryByRole('button', { name: 'Reasoning effort' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Model' })).toHaveTextContent('Second model · Account')
})

it('filters a dynamic catalog while keeping the selected model and default action reachable', () => {
  const onDefault = vi.fn()
  const catalog: ModelOptions = { providers: [{ id: 'openai', label: 'Cloud', models: Array.from({ length: 12 }, (_, i) => `model-${i}`) }] }
  render(<ModelPicker value={{ provider: 'openai', model_name: 'model-0', reasoning_effort: 'medium' }} catalog={catalog}
    onChange={vi.fn()} onFollowDefault={onDefault} />)
  fireEvent.click(screen.getByRole('button', { name: 'Model' }))
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search models' }), { target: { value: 'model-11' } })
  expect(screen.getByRole('option', { name: 'model-0 · Cloud' })).toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'model-11 · Cloud' })).toBeInTheDocument()
  expect(screen.queryByRole('option', { name: 'model-2 · Cloud' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('option', { name: 'Follow the default model' }))
  expect(onDefault).toHaveBeenCalledOnce()
})

it('does not replace saved settings when catalog entries disappear', () => {
  const settings = { llm: { provider: 'openai', model_name: 'saved:model', reasoning_effort: 'high' },
    permissions: {}, mcp: {}, memory: {}, browser: {}, tools: {}, sandbox: {}, identity: {}, api_keys: {}, speech_to_text: {},
  } as unknown as AgentSettings
  expect(normalizeDraft(settings, { providers: [] }).llm).toMatchObject(settings.llm)
})

it('keeps startup quiet and offers retry without replacing the saved selection', () => {
  const value = { provider: 'openai' as const, model_name: 'saved:model', reasoning_effort: 'medium' as const }
  const onChange = vi.fn()
  const onRetry = vi.fn()
  const { rerender } = render(<ModelPicker value={value} catalog={{ providers: [] }} onChange={onChange} onRetry={onRetry} loading />)
  expect(screen.getByRole('button', { name: 'Model' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Model' })).toHaveTextContent('Loading models…')
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  expect(screen.queryByText(/saved:model|openai/)).not.toBeInTheDocument()

  rerender(<ModelPicker value={value} catalog={{ providers: [] }} onChange={onChange} onRetry={onRetry} />)
  expect(screen.getByRole('button', { name: 'Model' })).toHaveTextContent('Saved model')
  expect(screen.getByRole('status')).toHaveTextContent('Couldn’t load models.')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(onRetry).toHaveBeenCalledOnce()
  expect(onChange).not.toHaveBeenCalled()

  const catalog: ModelOptions = { providers: [{
    id: 'openai', label: 'OpenAI API', models: ['saved:model'], labels: { 'saved:model': 'Saved choice' },
  }] }
  rerender(<ModelPicker value={value} catalog={catalog} onChange={onChange} onRetry={onRetry} />)
  expect(screen.getByRole('button', { name: 'Model' })).toHaveTextContent('Saved choice · OpenAI API')
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
})
