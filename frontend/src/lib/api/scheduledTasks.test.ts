import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiFetch } from './client'
import { deleteConversation } from './conversations'
import { runScheduledTaskNow, updateScheduledTask } from './scheduledTasks'

vi.mock('./client', async (original) => ({ ...await original<typeof import('./client')>(), apiFetch: vi.fn() }))
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
afterEach(() => vi.resetAllMocks())

describe('scheduled action reconciliation', () => {
  it('reuses the operation key after a lost response, checking before retrying acceptance', async () => {
    vi.mocked(apiFetch).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(runScheduledTaskNow('lost')).rejects.toThrow('Failed to fetch')
    const originalKey = new Headers(vi.mocked(apiFetch).mock.calls[0][1]?.headers).get('Idempotency-Key')
    expect(originalKey).toBeTruthy()
    vi.mocked(apiFetch).mockResolvedValueOnce(response({}, 404)).mockResolvedValueOnce(response({ runId: 1, conversationId: 'one' }))
    await expect(runScheduledTaskNow('lost')).resolves.toEqual({ runId: 1, conversationId: 'one' })
    expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain(`/operations/${originalKey}`)
    expect(new Headers(vi.mocked(apiFetch).mock.calls[2][1]?.headers).get('Idempotency-Key')).toBe(originalKey)
  })

  it('reconciles an accepted run without another POST, then permits a new explicit intention', async () => {
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error('Connection interrupted'))
    await expect(runScheduledTaskNow('accepted')).rejects.toThrow()
    const originalKey = new Headers(vi.mocked(apiFetch).mock.calls[0][1]?.headers).get('Idempotency-Key')
    vi.mocked(apiFetch).mockResolvedValueOnce(response({ runId: 2, conversationId: 'two' }))
    expect(await runScheduledTaskNow('accepted')).toEqual({ runId: 2, conversationId: 'two' })
    expect(apiFetch).toHaveBeenCalledTimes(2)
    vi.mocked(apiFetch).mockResolvedValueOnce(response({ runId: 3, conversationId: 'three' }))
    await runScheduledTaskNow('accepted')
    expect(new Headers(vi.mocked(apiFetch).mock.calls[2][1]?.headers).get('Idempotency-Key')).not.toBe(originalKey)
  })

  it('shares pending acceptance across callers and does not POST when reconciliation is unavailable', async () => {
    let reject!: (reason: Error) => void
    vi.mocked(apiFetch).mockReturnValueOnce(new Promise((_, rejectRequest) => { reject = rejectRequest }))
    const first = runScheduledTaskNow('shared')
    const second = runScheduledTaskNow('shared')
    expect(first).toBe(second)
    expect(apiFetch).toHaveBeenCalledTimes(1)
    reject(new Error('Lost'))
    await expect(first).rejects.toThrow()
    await expect(second).rejects.toThrow()
    vi.mocked(apiFetch).mockResolvedValueOnce(response({ message: 'Backend unavailable', code: 'unavailable' }, 503))
    await expect(runScheduledTaskNow('shared')).rejects.toBeInstanceOf(ApiError)
    expect(apiFetch).toHaveBeenCalledTimes(2)
    expect(vi.mocked(apiFetch).mock.calls[1][1]?.method).not.toBe('POST')
  })

  it('retains structured reasons for schedule and conversation failures', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(response({ code: 'conflict', message: 'Reload this schedule', request_id: 'req-1' }, 409))
    await expect(updateScheduledTask('schedule', { enabled: false })).rejects.toMatchObject({ message: 'Reload this schedule', requestId: 'req-1', status: 409 })
    vi.mocked(apiFetch).mockResolvedValueOnce(response({ message: 'Deletion failed', code: 'internal_error' }, 500))
    await expect(deleteConversation('conversation')).rejects.toThrow('Deletion failed')
    vi.mocked(apiFetch).mockResolvedValueOnce(response({}, 404))
    await expect(deleteConversation('already-deleted')).resolves.toBeUndefined()
  })
})
