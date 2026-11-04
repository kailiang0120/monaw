import { cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchMessages } from '../lib/api/conversations'
import { chatStream } from '../lib/api/chatStream'
import { fetchChatRuns, sendSteeringMessage, stopChatRun } from '../lib/api/steering'
import { MessageBubble } from '../components/MessageBubble'
import { useChat } from './useChat'

vi.mock('../lib/api/chatStream', () => ({ chatStream: vi.fn() }))
vi.mock('../lib/api/conversations', () => ({
  fetchMessages: vi.fn(),
  fetchMessageToolCalls: vi.fn(),
}))
vi.mock('../lib/api/steering', () => ({
  sendSteeringMessage: vi.fn(),
  fetchChatRuns: vi.fn(),
  stopChatRun: vi.fn().mockResolvedValue(true),
  newRunId: vi.fn(() => 'run-legacy'),
}))

describe('legacy chat messages', () => {
  afterEach(() => {
    cleanup()
    vi.resetAllMocks()
  })

  it('loads and expands an old saved tool message without Run metadata', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    vi.mocked(fetchChatRuns).mockResolvedValue([])
    vi.mocked(sendSteeringMessage).mockResolvedValue({
      message_id: 'steer-1', conversation_id: 'legacy-chat', status: 'queued',
    })
    vi.mocked(stopChatRun).mockResolvedValue(true)
    vi.mocked(fetchMessages).mockResolvedValue({
      messages: [{
        id: 41,
        role: 'assistant',
        content: 'I checked the project files.',
        thinking: '',
        created_at: '2025-01-02T03:04:05Z',
        tool_calls: [{
          id: 8,
          tool_name: 'fs_read',
          input: '{"path":"README.md"}',
          output: 'Project notes',
          status: 'complete',
        }],
      }],
      has_more: false,
      next_before_id: null,
    } as any)

    const { result } = renderHook(() => useChat('legacy-chat'))
    await waitFor(() => expect(result.current.isLoadingHistory).toBe(false))

    const [message] = result.current.messages
    expect(message).toMatchObject({
      id: '41',
      role: 'assistant',
      content: 'I checked the project files.',
      toolCalls: [{ id: '8', tool: 'fs_read', output: 'Project notes', pending: false }],
    })
    expect(message.runStatus).toBeUndefined()

    render(<MessageBubble message={message} />)
    fireEvent.click(screen.getByRole('button', { name: 'Used tool' }))
    expect(screen.getByRole('list', { name: 'Tool calls' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /fs_read/ }))
    expect(screen.getByText('Project notes')).toBeInTheDocument()
  })
})
