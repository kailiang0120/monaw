import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchMessages } from '../lib/api/conversations'
import { chatStream } from '../lib/api/chatStream'
import { useChat } from './useChat'

vi.mock('../lib/api/chatStream', () => ({
  chatStream: vi.fn(),
}))

vi.mock('../lib/api/conversations', () => ({
  fetchMessages: vi.fn(),
}))

function savedMessage(id: number) {
  return {
    id,
    role: id % 2 === 0 ? 'user' : 'assistant',
    content: `message ${id}`,
    thinking: '',
    tool_calls: [],
    created_at: '2026-06-07T00:00:00Z',
  }
}

describe('useChat history pagination', () => {
  afterEach(() => {
    cleanup()
    vi.resetAllMocks()
  })

  it('stops offering older history when a page does not advance the cursor', async () => {
    vi.mocked(fetchMessages)
      .mockResolvedValueOnce({
        messages: [savedMessage(10), savedMessage(11), savedMessage(12)],
        has_more: true,
        next_before_id: 10,
      } as any)
      .mockResolvedValueOnce({
        messages: [savedMessage(10)],
        has_more: true,
        next_before_id: 10,
      } as any)

    const { result } = renderHook(() => useChat('conv-history'))

    await waitFor(() => expect(result.current.hasMoreHistory).toBe(true))

    await act(async () => {
      await result.current.loadOlderMessages()
    })

    expect(fetchMessages).toHaveBeenNthCalledWith(
      2,
      'conv-history',
      30,
      10,
    )
    expect(result.current.messages.map((message) => message.id)).toEqual(['10', '11', '12'])
    expect(result.current.hasMoreHistory).toBe(false)
  })

  it('uses the server cursor for the next older-history page', async () => {
    vi.mocked(fetchMessages)
      .mockResolvedValueOnce({
        messages: [savedMessage(8), savedMessage(9), savedMessage(10)],
        has_more: true,
        next_before_id: 8,
      } as any)
      .mockResolvedValueOnce({
        messages: [savedMessage(5), savedMessage(6), savedMessage(7)],
        has_more: true,
        next_before_id: 5,
      } as any)

    const { result } = renderHook(() => useChat('conv-cursor'))

    await waitFor(() => expect(result.current.hasMoreHistory).toBe(true))

    await act(async () => {
      await result.current.loadOlderMessages()
    })

    expect(fetchMessages).toHaveBeenNthCalledWith(
      2,
      'conv-cursor',
      30,
      8,
    )
    expect(result.current.messages.map((message) => message.id)).toEqual(['5', '6', '7', '8', '9', '10'])
    expect(result.current.hasMoreHistory).toBe(true)
  })
})

describe('useChat answer handoff', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.resetAllMocks()
  })

  it('keeps the streamed answer mounted when the turn completes', async () => {
    vi.mocked(fetchMessages).mockResolvedValue({
      messages: [savedMessage(10)],
      has_more: false,
      next_before_id: null,
    } as any)
    vi.mocked(chatStream).mockReturnValue(vi.fn())

    const { result } = renderHook(() => useChat('conv-handoff'))
    await waitFor(() => expect(result.current.messages).toHaveLength(1))
    const historyFetches = vi.mocked(fetchMessages).mock.calls.length

    vi.useFakeTimers()
    act(() => result.current.sendMessage('Who are you?', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => {
      streamCall[3]('I am Monaw.')
      streamCall[6]({ conversation_id: 'conv-handoff', status: 'complete' })
    })

    expect(result.current.messages[result.current.messages.length - 1]).toMatchObject({
      content: 'I am Monaw.',
      streaming: false,
      runStatus: 'complete',
    })
    expect(result.current.isStreaming).toBe(false)
    expect(vi.mocked(fetchMessages)).toHaveBeenCalledTimes(historyFetches)
  })

  it('keeps a tool failure status for the activity display', async () => {
    vi.mocked(fetchMessages).mockResolvedValue({
      messages: [savedMessage(10)],
      has_more: false,
      next_before_id: null,
    } as any)
    vi.mocked(chatStream).mockReturnValue(vi.fn())

    const { result } = renderHook(() => useChat('conv-tool-status'))
    await waitFor(() => expect(result.current.messages).toHaveLength(1))

    vi.useFakeTimers()
    act(() => result.current.sendMessage('Run the checks', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => {
      streamCall[4]({ tool: 'exec', input: { command: 'npm test' }, call_id: 'call-1' })
      streamCall[5]({ output: 'Tests failed', call_id: 'call-1', status: 'error' })
      streamCall[6]({ conversation_id: 'conv-tool-status', status: 'complete' })
    })
    await act(async () => { vi.advanceTimersByTime(50) })

    const assistant = result.current.messages[result.current.messages.length - 1]
    expect(assistant.toolCalls?.[0]).toMatchObject({
      id: 'call-1',
      tool: 'exec',
      pending: false,
      status: 'error',
    })
    expect(assistant.activityItems?.find((item) => item.type === 'tool')).toMatchObject({
      toolCall: { id: 'call-1', status: 'error' },
    })
  })

  it('keeps a first reply visible while its new conversation id is applied', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    const { result, rerender } = renderHook(({ conversationId }) => useChat(conversationId), {
      initialProps: { conversationId: null as string | null },
    })

    vi.useFakeTimers()
    act(() => result.current.sendMessage('Who are you?', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => {
      streamCall[3]('I am Monaw.')
      streamCall[6]({ conversation_id: 'conv-new', status: 'complete' })
    })
    await act(async () => { vi.advanceTimersByTime(50) })
    rerender({ conversationId: 'conv-new' })

    expect(result.current.messages[result.current.messages.length - 1].content).toBe('I am Monaw.')
    expect(fetchMessages).not.toHaveBeenCalled()
  })
})
