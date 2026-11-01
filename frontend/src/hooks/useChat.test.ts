import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../lib/api/client'
import { fetchMessages } from '../lib/api/conversations'
import { chatStream } from '../lib/api/chatStream'
import { fetchChatRuns, newRunId, sendSteeringMessage, stopChatRun } from '../lib/api/steering'
import { useChat } from './useChat'

vi.mock('../lib/api/chatStream', () => ({
  chatStream: vi.fn(),
}))

vi.mock('../lib/api/conversations', () => ({
  fetchMessages: vi.fn(),
}))

vi.mock('../lib/api/steering', () => ({
  sendSteeringMessage: vi.fn(),
  fetchChatRuns: vi.fn(),
  stopChatRun: vi.fn().mockResolvedValue(true),
  newRunId: vi.fn(() => 'run-fixed'),
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

  it('passes a new chat working folder to the stream request', () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    const { result } = renderHook(() => useChat(null))
    act(() => result.current.sendMessage('Work on this project', [], vi.fn(), 'C:\\Projects\\demo'))
    const args = vi.mocked(chatStream).mock.calls[0]
    expect(args[1]).toBeNull()
    expect(args[25]).toBe('C:\\Projects\\demo')
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

  it('replaces a pending approval with the review decision and preserves other tickets', () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    const { result } = renderHook(() => useChat(null))
    act(() => result.current.sendMessage('Draft the report', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => {
      streamCall[8]?.({ ticket_id: 'a', action: 'Send report', reason: 'Sensitive action' })
      streamCall[8]?.({ ticket_id: 'b', action: 'Write draft', reason: 'Unclear target' })
      streamCall[9]?.({ ticket_id: 'a', action: 'Send report', reason: 'Draft only.', decision: 'deny', resolved_by: 'auto_review' })
    })
    const notices = result.current.messages.find((m) => m.role === 'assistant')!.approvals!
    expect(notices).toHaveLength(2)
    expect(notices[0]).toMatchObject({ type: 'resolved', decision: 'deny', reason: 'Draft only.' })
    expect(notices[1]).toMatchObject({ ticket_id: 'b', type: 'required' })
    act(() => streamCall[10]?.({ ticket_id: 'b', action: 'Write draft', reason: 'Unclear target', decision: 'approve' }))
    expect(result.current.messages.find((m) => m.role === 'assistant')!.approvals![1].type).toBe('resumed')
  })

  it('steers a new chat, handles applied-before-ack, and keeps one running assistant', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    const { result } = renderHook(() => useChat(null))
    act(() => result.current.sendMessage('Summarize the report', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => {
      streamCall[23]?.({ run_id: 'run-a', conversation_id: 'new-chat' })
      streamCall[3]('Old answer')
    })
    expect(result.current.steeringConversationId).toBe('new-chat')
    vi.mocked(sendSteeringMessage).mockImplementation(async (runId, messageId, message, attachments) => {
      streamCall[24]?.({ message_id: messageId, message, attachments, reset_response: true })
      return { message_id: messageId, conversation_id: 'new-chat', status: 'queued' }
    })
    await act(async () => { await result.current.steerMessage('Use a table') })
    expect(sendSteeringMessage).toHaveBeenCalledWith('run-a', expect.any(String), 'Use a table', [])
    expect(result.current.messages.filter((item) => item.role === 'assistant')).toHaveLength(1)
    expect(result.current.messages.filter((item) => item.content === 'Use a table')).toHaveLength(1)
    expect(result.current.messages[1]).toMatchObject({ role: 'user', steeringStatus: 'applied' })
    expect(result.current.messages[2].content).toBe('')
    expect(result.current.isStreaming).toBe(true)
    act(() => {
      streamCall[3]('Here is the table')
      streamCall[6]({ conversation_id: 'new-chat', status: 'complete' })
    })
    expect(result.current.messages[2]).toMatchObject({ content: 'Here is the table', runStatus: 'complete' })
  })

  it('shows a steer where the agent read it, with the reply continuing below', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    const { result } = renderHook(() => useChat(null))
    act(() => result.current.sendMessage('Write the notes', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => {
      streamCall[23]?.({ run_id: 'run-s', conversation_id: 'chat-s' })
      streamCall[4]({ tool: 'fs_write', input: { path: 'note-01.txt' }, call_id: 'w1' })
      streamCall[5]({ output: 'ok', call_id: 'w1', status: 'ok' })
    })
    vi.mocked(sendSteeringMessage).mockImplementation(async (_runId, messageId) => (
      { message_id: messageId, conversation_id: 'chat-s', status: 'queued' }
    ))
    await act(async () => { await result.current.steerMessage('Add cubes too') })
    // Not read yet: it waits below the reply so far.
    expect(result.current.messages.map((m) => [m.role, m.content || m.toolCalls?.[0]?.tool || ''])).toEqual([
      ['user', 'Write the notes'], ['assistant', 'fs_write'], ['user', 'Add cubes too'],
    ])
    const steerId = result.current.messages[2].id
    const turnStartedAtMs = result.current.messages[1].responseStartedAtMs

    act(() => {
      streamCall[24]?.({ message_id: steerId, message: 'Add cubes too', attachments: [], reset_response: true })
      streamCall[4]({ tool: 'fs_write', input: { path: 'note-02.txt' }, call_id: 'w2' })
      streamCall[3]('Done, with cubes.')
      streamCall[6]({ conversation_id: 'chat-s', status: 'complete' })
    })
    const [, before, steer, after] = result.current.messages
    expect(result.current.messages).toHaveLength(4)
    expect(before).toMatchObject({ role: 'assistant', streaming: false, runStatus: 'complete' })
    expect(before.toolCalls?.map((call) => call.id)).toEqual(['w1'])
    expect(steer).toMatchObject({ role: 'user', content: 'Add cubes too', steeringStatus: 'applied' })
    expect(after).toMatchObject({ role: 'assistant', content: 'Done, with cubes.', runStatus: 'complete' })
    expect(after.toolCalls?.map((call) => call.id)).toEqual(['w2'])
    // One task: the timer moves below the steer and keeps counting from the start.
    expect(before.responseStartedAtMs).toBeUndefined()
    expect(before.responseDurationMs).toBeUndefined()
    expect(after.responseStartedAtMs).toBe(turnStartedAtMs)
  })

  it('moves a steer the turn never read into the queue when the turn ends', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    vi.mocked(fetchMessages).mockResolvedValue({ messages: [], has_more: false, next_before_id: null } as any)
    const { result } = renderHook(() => useChat('chat-q'))
    await waitFor(() => expect(result.current.isLoadingHistory).toBe(false))
    act(() => result.current.sendMessage('Summarize', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => { streamCall[23]?.({ run_id: 'run-q', conversation_id: 'chat-q' }) })
    vi.mocked(sendSteeringMessage).mockImplementation(async (_runId, messageId) => (
      { message_id: messageId, conversation_id: 'chat-q', status: 'queued' }
    ))
    await act(async () => { await result.current.steerMessage('Add a chart') })
    expect(result.current.messages.some((m) => m.content === 'Add a chart' && m.steeringStatus === 'queued')).toBe(true)

    act(() => { streamCall[6]({ conversation_id: 'chat-q', status: 'complete' }) })
    expect(result.current.messages.some((m) => m.content === 'Add a chart')).toBe(false)
    expect(result.current.queued).toEqual([expect.objectContaining({ text: 'Add a chart', conversationId: 'chat-q' })])
    expect(result.current.queuePaused).toBe(false)
  })

  it('queues a message for after the turn, and Stop holds the queue until the next send', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    vi.mocked(fetchMessages).mockResolvedValue({ messages: [], has_more: false, next_before_id: null } as any)
    const { result } = renderHook(() => useChat('chat-hold'))
    await waitFor(() => expect(result.current.isLoadingHistory).toBe(false))
    act(() => result.current.sendMessage('Long task', [], vi.fn()))
    vi.mocked(stopChatRun).mockResolvedValue(true)
    act(() => result.current.queueMessage('Then write tests'))
    expect(result.current.queued).toEqual([expect.objectContaining({ text: 'Then write tests', conversationId: 'chat-hold' })])

    act(() => result.current.stopStreaming())
    expect(result.current.queuePaused).toBe(true)
    act(() => result.current.removeQueued(result.current.queued[0].id))
    act(() => result.current.sendMessage('Something else', [], vi.fn()))
    expect(result.current.queuePaused).toBe(false)
    expect(result.current.queued).toEqual([])
  })

  it('follows a turn still running when its chat is opened, and can stop it', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    vi.mocked(stopChatRun).mockResolvedValue(true)
    vi.mocked(fetchMessages).mockResolvedValue({ messages: [savedMessage(20)], has_more: false, next_before_id: null } as any)
    vi.mocked(fetchChatRuns).mockResolvedValue([
      { run_id: 'run-other', conversation_id: 'chat-other', message: 'Elsewhere', attachments: [], started_at: Date.now() / 1000 },
      { run_id: 'run-bg', conversation_id: 'chat-bg', message: 'Build the report', attachments: [], started_at: Date.now() / 1000 - 30 },
    ])
    const { result } = renderHook(() => useChat('chat-bg'))
    await waitFor(() => expect(result.current.isStreaming).toBe(true))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    expect(streamCall[26]).toBe('run-bg')
    expect(streamCall[27]).toBe('run-bg')
    expect(result.current.messages.map((m) => m.content)).toEqual(['message 20', 'Build the report', ''])
    expect(result.current.messages[2].responseStartedAtMs).toBeLessThan(Date.now() - 25_000)

    // Replayed events fill in the reply; Stop ends the followed run.
    act(() => {
      streamCall[23]?.({ run_id: 'run-bg', conversation_id: 'chat-bg' })
      streamCall[3]('Halfway there')
    })
    expect(result.current.steeringConversationId).toBe('chat-bg')
    act(() => result.current.stopStreaming())
    expect(stopChatRun).toHaveBeenCalledWith('run-bg')
  })

  it('reloads history when the followed turn ended before it reconnected', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    vi.mocked(fetchMessages).mockResolvedValue({ messages: [savedMessage(20)], has_more: false, next_before_id: null } as any)
    vi.mocked(fetchChatRuns).mockResolvedValueOnce([
      { run_id: 'run-gone', conversation_id: 'chat-gone', message: 'Done already', attachments: [], started_at: Date.now() / 1000 },
    ]).mockResolvedValue([])
    const { result } = renderHook(() => useChat('chat-gone'))
    await waitFor(() => expect(result.current.isStreaming).toBe(true))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => streamCall[7]('This turn is no longer running.', { code: 'not_found' }))
    await waitFor(() => expect(fetchMessages).toHaveBeenCalledTimes(2))
    expect(result.current.isStreaming).toBe(false)
    expect(result.current.messages.map((m) => m.content)).toEqual(['message 20'])
  })

  it('queues a steer that reaches a turn that already closed', async () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    vi.mocked(fetchMessages).mockResolvedValue({ messages: [], has_more: false, next_before_id: null } as any)
    const { result } = renderHook(() => useChat('chat-closed'))
    await waitFor(() => expect(result.current.isLoadingHistory).toBe(false))
    act(() => result.current.sendMessage('Summarize', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => { streamCall[23]?.({ run_id: 'run-c', conversation_id: 'chat-closed' }) })
    vi.mocked(sendSteeringMessage).mockRejectedValue(new ApiError(new Response(null, { status: 409 }), { code: 'conflict', message: '', request_id: '', details: {} }, 'This turn is no longer running.'))
    await act(async () => { await result.current.steerMessage('One more thing') })
    expect(result.current.queued).toEqual([expect.objectContaining({ text: 'One more thing' })])
  })

  it('cancels an old turn when starting a new chat and ignores late completion', () => {
    const abort = vi.fn()
    const onConversationCreated = vi.fn()
    vi.mocked(chatStream).mockReturnValue(abort)
    const { result } = renderHook(() => useChat(null))
    act(() => result.current.sendMessage('First chat', [], onConversationCreated))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => result.current.clearMessages())
    expect(abort).toHaveBeenCalledTimes(1)
    // New chat stops watching; the old turn keeps running on the backend.
    expect(stopChatRun).not.toHaveBeenCalled()

    act(() => {
      streamCall[3]('Late token')
      streamCall[4]({ tool: 'exec', input: {}, call_id: 'late-call' })
      streamCall[7]('Late error')
      streamCall[18]?.({ ticket_id: 'late-ticket', target_type: 'path', target_identifier: 'old-path', display_name: 'Old path', action_context: 'Read' })
      streamCall[6]({ conversation_id: 'old-chat', status: 'complete' })
    })
    expect(result.current.messages).toEqual([])
    expect(result.current.isStreaming).toBe(false)
    expect(result.current.pendingAccessGrant).toBeNull()
    expect(onConversationCreated).not.toHaveBeenCalled()
  })

  it('stops the old stream and loads history when switching chats', async () => {
    const abort = vi.fn()
    vi.mocked(chatStream).mockReturnValue(abort)
    vi.mocked(fetchMessages).mockResolvedValue({ messages: [savedMessage(20)], has_more: false, next_before_id: null } as any)
    const { result, rerender, unmount } = renderHook(({ id }) => useChat(id), {
      initialProps: { id: 'chat-a' },
    })
    await waitFor(() => expect(result.current.isLoadingHistory).toBe(false))
    act(() => result.current.sendMessage('Keep working', [], vi.fn()))
    rerender({ id: 'chat-b' })
    await waitFor(() => expect(result.current.isStreaming).toBe(false))
    expect(abort).toHaveBeenCalledTimes(1)
    expect(stopChatRun).not.toHaveBeenCalled()
    await waitFor(() => expect(result.current.messages.map((message) => message.id)).toEqual(['20']))
    expect(fetchMessages).toHaveBeenLastCalledWith('chat-b', 5, undefined, expect.any(AbortSignal))
    act(() => result.current.sendMessage('Continue here', [], vi.fn()))
    unmount()
    expect(abort).toHaveBeenCalledTimes(2)
  })

  it('sends a run id with the turn and stops that run when Stop is pressed', () => {
    const abort = vi.fn()
    vi.mocked(chatStream).mockReturnValue(abort)
    vi.mocked(newRunId).mockReturnValue('run-fixed')
    vi.mocked(stopChatRun).mockResolvedValue(true)
    const { result } = renderHook(() => useChat(null))
    act(() => result.current.sendMessage('Hello', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    expect(streamCall[26]).toBe('run-fixed')
    expect(streamCall[27]).toBeUndefined()
    act(() => result.current.stopStreaming())
    expect(stopChatRun).toHaveBeenCalledWith('run-fixed')
    expect(abort).toHaveBeenCalledTimes(1)
  })

  it('keeps the partial answer and error status after an interrupted stream', () => {
    vi.mocked(chatStream).mockReturnValue(vi.fn())
    const { result } = renderHook(() => useChat(null))
    act(() => result.current.sendMessage('Hello', [], vi.fn()))
    const streamCall = vi.mocked(chatStream).mock.calls[0]
    act(() => {
      streamCall[3]('Partial answer')
      streamCall[7]('Connection ended early', { code: 'stream_interrupted' })
    })
    expect(result.current.messages[result.current.messages.length - 1]).toMatchObject({
      content: 'Partial answer', streaming: false, runStatus: 'error',
    })
    expect(result.current.isStreaming).toBe(false)
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
