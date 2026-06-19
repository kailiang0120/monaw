import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiFetch, resetApiSessionForTests } from './client'

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
})
