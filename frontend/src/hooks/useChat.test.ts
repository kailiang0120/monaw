import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchMessages } from '../lib/api/conversations'
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
