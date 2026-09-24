import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { InputBar } from './InputBar'
import { FALLBACK_MODEL_OPTIONS } from '../features/settings/settingsConfig'

describe('InputBar model picker', () => {
  afterEach(cleanup)

  it('hides Google models until a Google API key is saved', () => {
    const common = {
      onSend: vi.fn(),
      onStop: vi.fn(),
      isStreaming: false,
      conversationId: null,
      contextRefreshKey: 0,
      approvalMode: 'default' as const,
      onApprovalModeChange: vi.fn(),
      modelOptions: FALLBACK_MODEL_OPTIONS,
      onModelSelectionChange: vi.fn(),
      modelSelection: {
        provider: 'openai' as const,
        model_name: 'gpt-6-luna',
        reasoning_effort: 'medium' as const,
      },
    }
    const { rerender } = render(<InputBar {...common} hasGoogleKey={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Model' }))
    expect(screen.queryByRole('option', { name: /Google ·/ })).not.toBeInTheDocument()

    rerender(<InputBar {...common} hasGoogleKey />)
    expect(screen.getByRole('option', { name: /Google · gemini-3.1-pro-preview/ })).toBeInTheDocument()
  })

  it('blocks a previously selected Google model when its key is removed', () => {
    const onSend = vi.fn()
    render(<InputBar
      onSend={onSend}
      onStop={vi.fn()}
      isStreaming={false}
      conversationId={null}
      contextRefreshKey={0}
      approvalMode="default"
      onApprovalModeChange={vi.fn()}
      modelOptions={FALLBACK_MODEL_OPTIONS}
      onModelSelectionChange={vi.fn()}
      modelSelection={{
        provider: 'gemini', model_name: 'gemini-3.1-pro-preview', reasoning_effort: 'medium',
      }}
      hasGoogleKey={false}
    />)

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: 'Hello' } })
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('Add a Google API key in Settings')
    expect(onSend).not.toHaveBeenCalled()
  })

  it('offers GPT-6 models in the composer and limits Astra effort choices', () => {
    const onModelSelectionChange = vi.fn()
    const common = {
      onSend: vi.fn(),
      onStop: vi.fn(),
      isStreaming: false,
      conversationId: null,
      contextRefreshKey: 0,
      approvalMode: 'default' as const,
      onApprovalModeChange: vi.fn(),
      modelOptions: FALLBACK_MODEL_OPTIONS,
      onModelSelectionChange,
    }
    const { rerender } = render(<InputBar {...common} modelSelection={{
      provider: 'openai', model_name: 'gpt-6-luna', reasoning_effort: 'none',
    }} />)

    expect(screen.getByPlaceholderText('Enter to send · Shift+Enter for newline')).toBeInTheDocument()
    expect(screen.getByLabelText('Context usage available after the first message')).toBeInTheDocument()

    rerender(<InputBar {...common} focusRequestKey={1} modelSelection={{
      provider: 'openai', model_name: 'gpt-6-luna', reasoning_effort: 'none',
    }} />)
    const composer = screen.getByPlaceholderText('Enter to send · Shift+Enter for newline')
    expect(composer).toHaveFocus()
    fireEvent.change(composer, { target: { value: 'Typing after Settings closes' } })
    expect(composer).toHaveValue('Typing after Settings closes')

    fireEvent.click(screen.getByRole('button', { name: 'Model' }))
    expect(screen.getByRole('option', { name: 'OpenAI account · gpt-6-astra' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: 'OpenAI API · gpt-6-astra' }))
    expect(onModelSelectionChange).toHaveBeenCalledWith({
      provider: 'openai', model_name: 'gpt-6-astra', reasoning_effort: 'none',
    })

    rerender(<InputBar {...common} modelSelection={{
      provider: 'openai', model_name: 'gpt-6-astra', reasoning_effort: 'low',
    }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Reasoning effort' }))
    expect(screen.queryByRole('option', { name: 'None' })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Max' })).toBeInTheDocument()

    rerender(<InputBar {...common} modelSelection={{
      provider: 'codex', model_name: 'gpt-6-astra', reasoning_effort: 'high',
    }} />)
    expect(screen.getByRole('option', { name: 'Ultra' })).toBeInTheDocument()
  })
})
