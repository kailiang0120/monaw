import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, apiFetch, decodeApiError, resetApiSessionForTests } from './client'

describe('authenticated API client', () => {
  afterEach(() => {
    resetApiSessionForTests()
    delete (window as any).electronAPI
    vi.restoreAllMocks()
  })

  it('adds the Electron-issued bearer token', async () => {
    ;(window as any).electronAPI = {
      getControlSession: vi.fn().mockResolvedValue({
        token: 'session-one',
        expiresAt: Date.now() + 60_000,
      }),
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{}', { status: 200 }),
    )

    await apiFetch('http://127.0.0.1:8435/api/settings')

    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers)
    expect(headers.get('Authorization')).toBe('Bearer session-one')
  })

  it('refreshes the session once after a 401 response', async () => {
    const getControlSession = vi.fn()
      .mockResolvedValueOnce({ token: 'expired', expiresAt: Date.now() + 60_000 })
      .mockResolvedValueOnce({ token: 'refreshed', expiresAt: Date.now() + 60_000 })
    ;(window as any).electronAPI = { getControlSession }
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response('{}', { status: 401 }))
      .mockResolvedValueOnce(new Response('{}', { status: 200 }))

    const response = await apiFetch('http://127.0.0.1:8435/api/settings')

    expect(response.status).toBe(200)
    expect(getControlSession).toHaveBeenCalledTimes(2)
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get('Authorization'))
      .toBe('Bearer refreshed')
  })

  it('decodes structured API error envelopes', async () => {
    const response = new Response(
      JSON.stringify({
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-123',
        details: { errors: [{ loc: ['body', 'mode'], type: 'string_pattern_mismatch' }] },
      }),
      { status: 422 },
    )

    const error = await decodeApiError(response, 'Fallback')

    expect(error).toBeInstanceOf(ApiError)
    expect(error.message).toBe('Request validation failed')
    expect(error.status).toBe(422)
    expect(error.code).toBe('validation_error')
    expect(error.requestId).toBe('req-123')
    expect(error.details.errors).toEqual([{ loc: ['body', 'mode'], type: 'string_pattern_mismatch' }])
  })

  it('falls back to legacy detail errors', async () => {
    const response = new Response(JSON.stringify({ detail: 'Ticket not found' }), {
      status: 404,
      headers: { 'x-request-id': 'req-legacy' },
    })

    const error = await decodeApiError(response, 'Fallback')

    expect(error.message).toBe('Ticket not found')
    expect(error.code).toBe('request_failed')
    expect(error.requestId).toBe('req-legacy')
  })
})
