import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, resetApiSessionForTests } from './client'
import { fetchRun, fetchRunEvents, pauseRun, streamRunEvents } from './runs'
import type { RunEvent, RunSnapshot } from './runs'

const runSnapshot: RunSnapshot = {
  run_id: 'run-api-desktop',
  conversation_id: 'chat-runs-api',
  attempt: 1,
  revision: 6,
  status: 'needs_review',
  reason_code: 'external_effect_unknown',
  available_actions: ['inspect', 'verify', 'stop'],
  last_seq: 12,
  created_at: '2026-11-03T01:00:00Z',
  updated_at: '2026-11-03T01:05:00Z',
  finished_at: null,
}

function routeEvent(seq: number, type: string, payload: Record<string, unknown>): RunEvent {
  return {
    run_id: 'run-api-desktop',
    seq,
    type,
    at: '2026-11-03T01:00:00Z',
    payload,
  }
}

function useSession() {
  ;(window as any).electronAPI = {
    getControlSession: vi.fn().mockResolvedValue({
      token: 'desktop-session-token',
      expiresAt: Date.now() + 60_000,
    }),
  }
}

describe('Run API transport', () => {
  afterEach(() => {
    resetApiSessionForTests()
    delete (window as any).electronAPI
    vi.restoreAllMocks()
  })

  it('reads the same snapshot and event-page DTOs returned by the Run routes', async () => {
    useSession()
    const event = routeEvent(13, 'operation.updated', {
      operation_id: 'op-2', status: 'verified', sequence: 2, attempt: 1,
      reason_code: null, evidence: { verification_result: 'completed' }, revision: 7,
    })
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify(runSnapshot), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        run_id: 'run-api-desktop', events: [event], next_seq: 13, last_seq: 13,
      }), { status: 200 }))

    await expect(fetchRun('run-api-desktop')).resolves.toEqual(runSnapshot)
    await expect(fetchRunEvents('run-api-desktop', 12, 100)).resolves.toEqual({
      run_id: 'run-api-desktop', events: [event], next_seq: 13, last_seq: 13,
    })
    expect(String(fetchMock.mock.calls[1][0])).toContain('/api/runs/run-api-desktop/events?after_seq=12&limit=100')
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get('Authorization'))
      .toBe('Bearer desktop-session-token')
  })

  it('preserves the backend events_expired envelope as an ApiError', async () => {
    useSession()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      code: 'events_expired',
      message: 'The requested Run events are no longer retained.',
      request_id: 'req-expired',
      details: {},
    }), { status: 410 }))

    const error = await fetchRunEvents('run-api-desktop', 0).catch((value: unknown) => value)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 410, code: 'events_expired', requestId: 'req-expired' })
  })

  it('decodes FastAPI nested detail errors without losing revision-conflict context', async () => {
    useSession()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      detail: {
        code: 'revision_conflict',
        message: 'The Run changed since the client last read it.',
        details: { current_run: { ...runSnapshot, revision: 7 } },
      },
    }), { status: 409 }))

    const error = await fetchRun('run-api-desktop').catch((value: unknown) => value)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 409,
      code: 'revision_conflict',
      message: 'The Run changed since the client last read it.',
      details: { current_run: { run_id: 'run-api-desktop', revision: 7 } },
    })
  })

  it('sends Run controls with the server revision, attempt and stable retry key', async () => {
    useSession()
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      accepted: true, run: { ...runSnapshot, status: 'pausing', revision: 7 },
    }), { status: 202 }))

    await pauseRun('run-api-desktop', runSnapshot, 'same-pause-key')

    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toContain('/api/runs/run-api-desktop/pause')
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBe('same-pause-key')
    expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 6, expected_attempt: 1 })
  })

  it('parses a real-route SSE frame split across transport chunks and ignores heartbeats', async () => {
    useSession()
    const event = routeEvent(14, 'operation.updated', {
      operation_id: 'op-2', status: 'verified', revision: 7,
    })
    const encoded = new TextEncoder().encode(
      `: keepalive\n\nevent: operation.updated\nid: 14\ndata: ${JSON.stringify(event)}\n\n`,
    )
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoded.slice(0, 21))
        controller.enqueue(encoded.slice(21, 58))
        controller.enqueue(encoded.slice(58))
        controller.close()
      },
    })
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }))
    const received: RunEvent[] = []

    await streamRunEvents('run-api-desktop', 13, (item) => { received.push(item) })

    expect(received).toEqual([event])
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/runs/run-api-desktop/events/stream?after_seq=13')
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers)).toMatchObject({})
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get('Authorization'))
      .toBe('Bearer desktop-session-token')
  })
})
