import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { InputBar } from './InputBar'
import { FALLBACK_MODEL_OPTIONS } from '../features/settings/settingsConfig'
import { uploadAttachment } from '../lib/api/uploads'

vi.mock('../lib/api/uploads', () => ({ uploadAttachment: vi.fn() }))

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
    expect(screen.getByRole('option', { name: /Google · gemini-pro-latest/ })).toBeInTheDocument()
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
        provider: 'gemini', model_name: 'gemini-pro-latest', reasoning_effort: 'medium',
      }}
      hasGoogleKey={false}
    />)

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: 'Hello' } })
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
    expect(screen.getByRole('status')).toHaveTextContent('Add a Google API key in Settings')
    expect(onSend).not.toHaveBeenCalled()
  })

  it.each(['modelSelectionDisabled', 'approvalModeDisabled'] as const)('waits for %s settings to save before sending', (savingProp) => {
    const onSend = vi.fn()
    const props = {
      onSend, onStop: vi.fn(), isStreaming: false, conversationId: null,
      contextRefreshKey: 0, approvalMode: 'default' as const, onApprovalModeChange: vi.fn(),
      modelOptions: FALLBACK_MODEL_OPTIONS, onModelSelectionChange: vi.fn(),
      modelSelection: { provider: 'openai' as const, model_name: 'gpt-6-luna', reasoning_effort: 'medium' as const },
    }
    const { rerender } = render(<InputBar {...props} {...{ [savingProp]: true }} />)
    const composer = screen.getByRole('textbox', { name: 'Message' })
    fireEvent.change(composer, { target: { value: 'Hello' } })
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
    fireEvent.keyDown(composer, { key: 'Enter' })
    fireEvent.change(composer, { target: { value: '/comp' } })
    fireEvent.keyDown(composer, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    rerender(<InputBar {...props} {...{ [savingProp]: false }} />)
    fireEvent.change(composer, { target: { value: 'Hello' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    expect(onSend).toHaveBeenCalledWith('Hello', [])
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

    fireEvent.click(screen.getByRole('button', { name: 'Permission mode' }))
    expect(screen.getByRole('option', { name: 'Default' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(screen.getByRole('option', { name: 'Full Access' }))
    expect(common.onApprovalModeChange).toHaveBeenCalledWith('full_access')
    expect(screen.queryByRole('listbox', { name: 'Permission mode' })).not.toBeInTheDocument()

    rerender(<InputBar {...common} focusRequestKey={1} modelSelection={{
      provider: 'openai', model_name: 'gpt-6-luna', reasoning_effort: 'none',
    }} />)
    const composer = screen.getByPlaceholderText('Enter to send · Shift+Enter for newline')
    expect(composer).toHaveFocus()
    fireEvent.change(composer, { target: { value: 'Typing after Settings closes' } })
    expect(composer).toHaveValue('Typing after Settings closes')

    fireEvent.click(screen.getByRole('button', { name: 'Model' }))
    expect(screen.getByRole('option', { name: 'OpenAI account · gpt-6-astra' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'OpenAI API · gpt-6.1-sol' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'OpenAI API · gpt-6-sol' })).not.toBeInTheDocument()
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

    rerender(<InputBar {...common} modelSelection={{
      provider: 'openai', model_name: 'gpt-6.1-sol', reasoning_effort: 'low',
    }} />)
    expect(screen.queryByRole('option', { name: 'None' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Ultra' })).not.toBeInTheDocument()

    rerender(<InputBar {...common} hasGoogleKey modelSelection={{
      provider: 'gemini', model_name: 'gemini-flash-latest', reasoning_effort: 'low',
    }} />)
    expect(screen.queryByRole('option', { name: 'Minimal' })).not.toBeInTheDocument()
    rerender(<InputBar {...common} hasGoogleKey modelSelection={{
      provider: 'gemini', model_name: 'gemini-flash-lite-latest', reasoning_effort: 'minimal',
    }} />)
    expect(screen.getByRole('option', { name: 'Minimal' })).toBeInTheDocument()
  })
})

describe('InputBar clipboard attachments', () => {
  const createObjectURL = vi.fn(() => 'blob:composer-preview')
  const revokeObjectURL = vi.fn()
  const BrowserURL = URL
  beforeEach(() => {
    vi.stubGlobal('URL', class extends BrowserURL {
      static createObjectURL = createObjectURL
      static revokeObjectURL = revokeObjectURL
    })
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  const props = {
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

  it('sends steering messages beside Stop and preserves the draft on delivery failure', async () => {
    const onSteer = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error('Turn finished'))
    render(<InputBar {...props} isStreaming onSteer={onSteer} steeringConversationId="running-chat" />)
    const composer = screen.getByRole('textbox', { name: 'Message' })
    const image = new File(['image'], 'steering.png', { type: 'image/png' })
    const attachment = { id: 'steer-image', name: image.name, path: 'attachment://steer-image', mime_type: image.type, size: image.size, conversation_id: 'running-chat' }
    vi.mocked(uploadAttachment).mockResolvedValueOnce(attachment)
    fireEvent.paste(composer, { clipboardData: { files: [image], items: [] } })
    fireEvent.change(composer, { target: { value: 'Use a table' } })
    fireEvent.keyDown(composer, { key: 'Enter' })
    await waitFor(() => expect(composer).toHaveValue(''))
    expect(onSteer).toHaveBeenCalledWith('Use a table', [attachment])
    expect(uploadAttachment).toHaveBeenCalledWith(image, 'running-chat')
    expect(props.onSend).not.toHaveBeenCalled()
    expect(props.onStop).not.toHaveBeenCalled()
    fireEvent.change(composer, { target: { value: 'Keep it short' } })
    fireEvent.click(screen.getByRole('button', { name: 'Steer message' }))
    expect(await screen.findByText('Turn finished')).toBeInTheDocument()
    expect(composer).toHaveValue('Keep it short')
    fireEvent.click(screen.getByRole('button', { name: 'Stop response' }))
    expect(props.onStop).toHaveBeenCalledTimes(1)
  })

  it.each(['files', 'items'] as const)('pastes images and documents from clipboard %s and sends them once', async (source) => {
    const image = new File(['image'], 'screenshot.png', { type: 'image/png' })
    const document = new File(['document'], 'report.pdf', { type: 'application/pdf' })
    const files = [image, document]
    const attachments = files.map((file, index) => ({
      id: `upload-${index}`, name: file.name, mime_type: file.type, size: file.size,
      path: `/uploads/${file.name}`, conversation_id: 'pending',
    }))
    vi.mocked(uploadAttachment).mockResolvedValueOnce(attachments[0]).mockResolvedValueOnce(attachments[1])
    render(<InputBar {...props} />)
    const composer = screen.getByRole('textbox', { name: 'Message' })
    fireEvent.change(composer, { target: { value: 'Please review these' } })
    const paste = new Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(paste, 'clipboardData', { value: {
      files: source === 'files' ? files : [],
      items: files.map((file) => ({ kind: 'file', getAsFile: () => file })),
    } })
    fireEvent(composer, paste)
    expect(paste.defaultPrevented).toBe(true)
    expect(composer).toHaveValue('Please review these')
    expect(screen.getAllByRole('button', { name: /^Remove / })).toHaveLength(2)
    const preview = screen.getByRole('img', { name: 'Preview of screenshot.png' })
    expect(preview).toHaveAttribute('src', 'blob:composer-preview')
    expect(createObjectURL).toHaveBeenCalledWith(image)
    expect(screen.queryByText('screenshot.png')).not.toBeInTheDocument()
    expect(screen.getByText('report.pdf')).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Attachments' }).compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(uploadAttachment).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    await waitFor(() => expect(props.onSend).toHaveBeenCalledWith('Please review these', attachments))
    expect(uploadAttachment).toHaveBeenCalledTimes(2)
    expect(uploadAttachment).toHaveBeenNthCalledWith(1, image, null)
    expect(uploadAttachment).toHaveBeenNthCalledWith(2, document, null)
    expect(screen.queryByRole('button', { name: /^Remove / })).not.toBeInTheDocument()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:composer-preview')
  })

  it('leaves normal text paste to the textarea', () => {
    render(<InputBar {...props} />)
    const paste = new Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(paste, 'clipboardData', { value: {
      files: [], items: [{ kind: 'string' }],
    } })
    fireEvent(screen.getByRole('textbox', { name: 'Message' }), paste)
    expect(paste.defaultPrevented).toBe(false)
    expect(screen.queryByRole('button', { name: /^Remove / })).not.toBeInTheDocument()
  })

  it('previews picked images and releases previews when removed or the composer closes', () => {
    const { container, unmount } = render(<InputBar {...props} />)
    const picker = container.querySelector('input[type="file"]')!
    const image = new File(['image'], 'photo.png')
    fireEvent.change(picker, { target: { files: [image] } })
    expect(screen.getByRole('img', { name: 'Preview of photo.png' })).toHaveAttribute('src', 'blob:composer-preview')
    fireEvent.click(screen.getByRole('button', { name: 'Remove photo.png' }))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(revokeObjectURL).toHaveBeenCalledTimes(1)
    fireEvent.change(picker, { target: { files: [image] } })
    unmount()
    expect(revokeObjectURL).toHaveBeenCalledTimes(2)
    expect(uploadAttachment).not.toHaveBeenCalled()
  })
})
