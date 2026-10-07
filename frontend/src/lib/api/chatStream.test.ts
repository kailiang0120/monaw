import { afterEach, describe, expect, it, vi } from 'vitest'
import { chatStream } from './chatStream'
import { apiFetch } from './client'

vi.mock('./client', async (importOriginal) => ({
  ...await importOriginal<typeof import('./client')>(),
  apiFetch: vi.fn(),
}))

describe('chat stream completion', () => {
  afterEach(() => vi.resetAllMocks())

  it('includes the selected folder in a new chat request', async () => {
    vi.mocked(apiFetch).mockResolvedValue(new Response('event: done\ndata: {"status":"complete"}\n\n'))
    const onDone = vi.fn()
    chatStream('Hello', null, [], vi.fn(), vi.fn(), vi.fn(), onDone, vi.fn(),
      undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, undefined, undefined, undefined, undefined, undefined,
      undefined, undefined, undefined, undefined, undefined, 'C:\\Projects\\demo')
    await vi.waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body))).toEqual({
      message: 'Hello', conversation_id: null, attachments: [], workspace_dir: 'C:\\Projects\\demo',
    })
  })

  it.each([false, true])('requires a terminal event to report success (completed: %s)', async (completed) => {
    const token = 'event: token\ndata: {"content":"Partial answer"}\n\n'
    const done = 'event: done\ndata: {"conversation_id":"chat-a","status":"complete"}\n\n'
    const started = 'event: turn_started\ndata: {"run_id":"run-a","conversation_id":"chat-a"}\n\n'
    const applied = 'event: steering_applied\ndata: {"message_id":"steer-a","message":"Use a table","reset_response":true}\n\n'
    vi.mocked(apiFetch).mockResolvedValue(new Response(started + applied + token + (completed ? done : '')))
    const onToken = vi.fn()
    const onDone = vi.fn()
    const onError = vi.fn()
    const onStarted = vi.fn()
    const onApplied = vi.fn()
    chatStream('Hello', 'chat-a', [], onToken, vi.fn(), vi.fn(), onDone, onError,
      undefined, undefined, undefined,
      undefined, undefined, undefined,
      undefined, undefined, undefined, undefined,
      undefined, undefined,
      undefined, undefined, undefined,
      onStarted, onApplied)
    await vi.waitFor(() => expect(onDone.mock.calls.length + onError.mock.calls.length).toBe(1))
    expect(onToken).toHaveBeenCalledWith('Partial answer')
    expect(onStarted).toHaveBeenCalledWith({ run_id: 'run-a', conversation_id: 'chat-a' })
    expect(onApplied).toHaveBeenCalledWith({ message_id: 'steer-a', message: 'Use a table', reset_response: true })
    if (completed) {
      expect(onDone).toHaveBeenCalledWith({ conversation_id: 'chat-a', status: 'complete' })
      expect(onError).not.toHaveBeenCalled()
    } else {
      expect(onDone).not.toHaveBeenCalled()
      expect(onError).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ code: 'stream_interrupted' }))
    }
  })
})
