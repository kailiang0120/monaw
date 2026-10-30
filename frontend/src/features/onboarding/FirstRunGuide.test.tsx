import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FirstRunGuide } from './FirstRunGuide'
import { EXAMPLE_PROMPT, shouldShowGuide, type GuideStep } from './readiness'

afterEach(cleanup)

const modelStep = (state: GuideStep['state']): GuideStep => ({
  id: 'model', title: 'Connect a model', detail: 'Add an API key for OpenAI API.', state,
  settingsPage: 'apiKeys', actionLabel: state === 'ready' ? undefined : 'Add key',
})

function renderGuide(overrides: Partial<Parameters<typeof FirstRunGuide>[0]> = {}) {
  const props = {
    required: [modelStep('action')],
    optional: [],
    modelReady: false,
    exampleDisabled: false,
    onOpenSettings: vi.fn(),
    onRunExample: vi.fn(),
    ...overrides,
  }
  render(<FirstRunGuide {...props} />)
  return props
}

describe('FirstRunGuide', () => {
  it('keeps the example task disabled until a model is connected and sends the read-only prompt', () => {
    renderGuide()
    expect(screen.getByRole('button', { name: 'Run example' })).toBeDisabled()
    expect(screen.getByText('Connect a model first.')).toBeInTheDocument()
    cleanup()

    const props = renderGuide({ required: [modelStep('ready')], modelReady: true })
    fireEvent.click(screen.getByRole('button', { name: 'Run example' }))
    expect(props.onRunExample).toHaveBeenCalledWith(EXAMPLE_PROMPT)
  })

  it('opens the settings page that resolves a step', () => {
    const props = renderGuide()
    fireEvent.click(screen.getByRole('button', { name: 'Add key: Connect a model' }))
    expect(props.onOpenSettings).toHaveBeenCalledWith('apiKeys')
  })

  it('lists turned-on capabilities with their checked state', () => {
    renderGuide({
      optional: [{ id: 'docker', title: 'Docker isolation', detail: 'Docker is not running. Commands are blocked until it is.', state: 'problem', settingsPage: 'sandbox', actionLabel: 'Details' }],
    })
    expect(screen.getByRole('heading', { name: 'Turned on' })).toBeInTheDocument()
    expect(screen.getByText(/Docker isolation/)).toHaveTextContent('(Problem)')
  })

  it('offers hiding only when the caller allows it', () => {
    renderGuide()
    expect(screen.queryByRole('button', { name: 'Hide guide' })).not.toBeInTheDocument()
    cleanup()
    const onDismiss = vi.fn()
    renderGuide({ modelReady: true, onDismiss })
    fireEvent.click(screen.getByRole('button', { name: 'Hide guide' }))
    expect(onDismiss).toHaveBeenCalled()
  })
})

describe('shouldShowGuide', () => {
  const base = { inEmptyNewChat: true, loaded: true, modelReady: true, dismissed: false, conversationsLoaded: true, conversationCount: 0 }

  it('shows on a true first run and stays away once there is history or it was hidden', () => {
    expect(shouldShowGuide(base)).toBe(true)
    expect(shouldShowGuide({ ...base, conversationCount: 2 })).toBe(false)
    expect(shouldShowGuide({ ...base, dismissed: true })).toBe(false)
    expect(shouldShowGuide({ ...base, conversationsLoaded: false })).toBe(false)
  })

  it('always shows while no model is connected, but only in an empty new chat', () => {
    const noModel = { ...base, modelReady: false, dismissed: true, conversationCount: 5 }
    expect(shouldShowGuide(noModel)).toBe(true)
    expect(shouldShowGuide({ ...noModel, inEmptyNewChat: false })).toBe(false)
    expect(shouldShowGuide({ ...noModel, loaded: false })).toBe(false)
  })
})
