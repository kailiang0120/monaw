import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetApiSessionForTests } from '../lib/api/client'
import type { RunEvent, RunEventsPage, RunSnapshot } from '../lib/api/runs'
import { useRunEvents } from './useRunEvents'

function snapshot(runId: string, lastSeq: number, status: RunSnapshot['status'] = 'running'): RunSnapshot {
  return {
    run_id: runId,
    conversation_id: `chat-${runId}`,
    attempt: 1,
    revision: lastSeq,
    status,
    reason_code: null,
    available_actions: status === 'running' ? ['pause', 'stop'] : ['inspect'],
    last_seq: lastSeq,
    created_at: '2026-11-03T01:00:00Z',
    updated_at: '2026-11-03T01:05:00Z',
    finished_at: status === 'completed' ? '2026-11-03T01:06:00Z' : null,
  }
}

function event(runId: string, seq: number): RunEvent {
  return {
    run_id: runId,
    seq,
    type: seq === 1 ? 'run.created' : 'operation.updated',
    at: '2026-11-03T01:00:00Z',
    payload: seq === 1
      ? { status: 'running', revision: 1 }
      : { operation_id: `op-${seq}`, status: 'verified', revision: seq },
  }
}

function eventPage(runId: string, events: RunEvent[], lastSeq: number): RunEventsPage {
  return {
    run_id: runId,
    events,
    next_seq: events.length ? events[events.length - 1].seq : 0,
    last_seq: lastSeq,
  }
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function eventStream(events: RunEvent[]): Response {
  const encoder = new TextEncoder()
  const frames = events.map((item) => `id: ${item.seq}\nevent: ${item.type}\ndata: ${JSON.stringify(item)}\n\n`)
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(`: keepalive\n\n${frames.join('')}`))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function installDesktopSession() {
  ;(window as any).electronAPI = {
    getControlSession: vi.fn().mockResolvedValue({ token: 'test-control-token', expiresAt: Date.now() + 60_000 }),
  }
}

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(String(input))
}

describe('useRunEvents durable replay and reconnection', () => {
  afterEach(() => {
    cleanup()
    resetApiSessionForTests()
    delete (window as any).electronAPI
    vi.restoreAllMocks()
  })

  it('replays every page over 500 events, bridges a disconnect, and deduplicates SSE replay', async () => {
    installDesktopSession()
    const runId = 'run-large-history'
    const history = Array.from({ length: 506 }, (_, index) => event(runId, index + 1))
    const eventPageCursors: number[] = []
    const streamCursors: number[] = []
    let streamCount = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = requestUrl(input)
      if (url.pathname.endsWith('/events/stream')) {
        const cursor = Number(url.searchParams.get('after_seq'))
        streamCursors.push(cursor)
        streamCount += 1
        if (streamCount === 1) return eventStream([history[501], history[502]])
        if (streamCount === 2) return eventStream([history[504], history[505]])
        return eventStream([])
      }
      if (url.pathname.endsWith('/events')) {
        const cursor = Number(url.searchParams.get('after_seq'))
        eventPageCursors.push(cursor)
        if (cursor === 0) return jsonResponse(eventPage(runId, history.slice(0, 500), 506))
        if (cursor === 500) return jsonResponse(eventPage(runId, history.slice(500, 502), 506))
        if (cursor === 503) return jsonResponse(eventPage(runId, history.slice(503, 505), 506))
        return jsonResponse(eventPage(runId, [], 506))
      }
      if (url.pathname === `/api/runs/${runId}`) {
        const latest = streamCount === 1 ? 503 : 506
        return jsonResponse(snapshot(runId, latest, latest === 506 ? 'completed' : 'running'))
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useRunEvents(runId))
    await waitFor(() => expect(result.current.lastSeq).toBe(506), { timeout: 5_000 })

    expect(eventPageCursors).toEqual([0, 500, 503])
    expect(streamCursors).toEqual([502, 505])
    expect(result.current.events.map((item) => item.seq)).toEqual(Array.from({ length: 506 }, (_, index) => index + 1))
    expect(new Set(result.current.events.map((item) => `${item.run_id}:${item.seq}`)).size).toBe(506)
    expect(result.current.events[0]).toMatchObject({ run_id: runId, seq: 1, type: 'run.created' })
    expect(result.current.events[505]).toMatchObject({ run_id: runId, seq: 506, type: 'operation.updated' })
  })

  it('falls back to the atomic snapshot when the requested event cursor has expired', async () => {
    installDesktopSession()
    const runId = 'run-expired-cursor'
    const current = snapshot(runId, 9, 'needs_review')
    const onSnapshot = vi.fn()
    const requests: string[] = []
    const nextEvent = event(runId, 10)
    let snapshotReads = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = requestUrl(input)
      requests.push(url.pathname + url.search)
      if (url.pathname.endsWith('/events/stream')) return eventStream([nextEvent])
      if (url.pathname.endsWith('/events')) {
        return jsonResponse({
          code: 'events_expired',
          message: 'The requested Run events are no longer retained.',
          request_id: 'req-expired',
          details: {},
        }, 410)
      }
      if (url.pathname === `/api/runs/${runId}`) {
        snapshotReads += 1
        return jsonResponse(snapshotReads === 1 ? current : snapshot(runId, 10, 'completed'))
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useRunEvents(runId, { onSnapshot }))
    await waitFor(() => expect(result.current.lastSeq).toBe(10))

    expect(requests[0]).toContain('/events?after_seq=0')
    expect(requests[1]).toBe(`/api/runs/${runId}`)
    expect(requests).toContain(`/api/runs/${runId}/events/stream?after_seq=9`)
    expect(onSnapshot).toHaveBeenCalledWith(current)
    expect(result.current.events).toEqual([nextEvent])
  })

  it('aborts the old chat subscription and ignores its late REST response', async () => {
    installDesktopSession()
    const oldRunId = 'run-old-chat'
    const newRunId = 'run-new-chat'
    let resolveOld!: (response: Response) => void
    let oldSignal: AbortSignal | undefined
    const newEvent = event(newRunId, 1)
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input)
      if (url.pathname === `/api/runs/${oldRunId}/events`) {
        oldSignal = init?.signal ?? undefined
        return new Promise<Response>((resolve) => { resolveOld = resolve })
      }
      if (url.pathname === `/api/runs/${newRunId}/events`) {
        return Promise.resolve(jsonResponse(eventPage(newRunId, [], 1)))
      }
      if (url.pathname === `/api/runs/${newRunId}/events/stream`) {
        return Promise.resolve(eventStream([newEvent]))
      }
      if (url.pathname === `/api/runs/${newRunId}`) {
        return Promise.resolve(jsonResponse(snapshot(newRunId, 1, 'completed')))
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result, rerender } = renderHook(({ runId }) => useRunEvents(runId), {
      initialProps: { runId: oldRunId },
    })
    await waitFor(() => expect(oldSignal).toBeDefined())
    rerender({ runId: newRunId })
    await waitFor(() => expect(result.current.events).toEqual([newEvent]))
    expect(oldSignal?.aborted).toBe(true)

    await act(async () => {
      resolveOld(jsonResponse(eventPage(oldRunId, [event(oldRunId, 1)], 1)))
    })

    expect(result.current.events).toEqual([newEvent])
    expect(result.current.lastSeq).toBe(1)
  })
})
