import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetApiSessionForTests } from '../lib/api/client'
import type { RunEvent, RunOperation, RunSnapshot } from '../lib/api/runs'
import { useRunController } from './useRunController'

function run(runId: string, conversationId: string): RunSnapshot {
  return {
    run_id: runId,
    conversation_id: conversationId,
    attempt: 1,
    revision: 1,
    status: 'completed',
    reason_code: null,
    available_actions: ['inspect'],
    last_seq: 1,
    created_at: '2026-11-03T01:00:00Z',
    updated_at: '2026-11-03T01:01:00Z',
    finished_at: '2026-11-03T01:01:00Z',
  }
}

function runAtRevision(
  runId: string,
  conversationId: string,
  revision: number,
  lastSeq: number,
  status: RunSnapshot['status'] = 'running',
): RunSnapshot {
  const availableActions: RunSnapshot['available_actions'] = status === 'running'
    ? ['pause', 'stop']
    : status === 'paused'
      ? ['resume', 'stop']
      : ['stop']
  return {
    ...run(runId, conversationId),
    revision,
    last_seq: lastSeq,
    status,
    available_actions: availableActions,
    updated_at: new Date(Date.UTC(2026, 10, 3, 1, revision)).toISOString(),
    finished_at: null,
  }
}

function operation(runId: string, operationId: string, sequence: number): RunOperation {
  const timestamp = '2026-11-03T01:10:00Z'
  return {
    run_id: runId,
    operation_id: operationId,
    sequence,
    attempt: 1,
    operation_kind: 'file_write',
    related_operation_id: null,
    effect_class: 'verifiable_write',
    status: 'verified',
    reason_code: null,
    input_summary: { path: 'note.txt' },
    evidence: { verification_result: 'completed' },
    snapshot_available: false,
    created_at: timestamp,
    updated_at: timestamp,
  }
}

function operationPage(runSnapshot: RunSnapshot, item: RunOperation) {
  return {
    run_id: runSnapshot.run_id,
    operations: [item],
    next_sequence: item.sequence,
    has_more: false,
    conversation_id: runSnapshot.conversation_id,
  }
}

function recoveryReport(runSnapshot: RunSnapshot, item: RunOperation) {
  return {
    run_id: runSnapshot.run_id,
    run: runSnapshot,
    unresolved_operations: [item],
    operations_next_sequence: item.sequence,
    operations_truncated: false,
  }
}

function controlledEventStream() {
  const encoder = new TextEncoder()
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null
  const response = new Response(new ReadableStream<Uint8Array>({
    start(streamController) {
      controller = streamController
      streamController.enqueue(encoder.encode(': connected\n\n'))
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
  return {
    response,
    publish(event: RunEvent) {
      controller?.enqueue(encoder.encode(
        `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`,
      ))
    },
    close() {
      try { controller?.close() } catch { /* The stream may already be closed. */ }
    },
  }
}

function eventPage(runId: string, lastSeq: number) {
  return { run_id: runId, events: [], next_seq: lastSeq, last_seq: lastSeq }
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function installDesktopSession() {
  ;(window as any).electronAPI = {
    getControlSession: vi.fn().mockResolvedValue({ token: 'profile-token', expiresAt: Date.now() + 60_000 }),
  }
}

describe('useRunController chat scoping', () => {
  afterEach(() => {
    cleanup()
    resetApiSessionForTests()
    delete (window as any).electronAPI
    vi.restoreAllMocks()
  })

  it('aborts a slow prior-chat list and ignores its response after switching conversations', async () => {
    installDesktopSession()
    const staleRun = run('run-chat-a', 'chat-a')
    const currentRun = run('run-chat-b', 'chat-b')
    let resolveChatA!: (response: Response) => void
    let chatASignal: AbortSignal | undefined
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/runs' && url.searchParams.get('conversation_id') === 'chat-a') {
        chatASignal = init?.signal ?? undefined
        return new Promise<Response>((resolve) => { resolveChatA = resolve })
      }
      if (url.pathname === '/api/runs' && url.searchParams.get('conversation_id') === 'chat-b') {
        return Promise.resolve(jsonResponse({ runs: [currentRun], next_run_id: null, has_more: false }))
      }
      if (url.pathname === '/api/runs/run-chat-b') return Promise.resolve(jsonResponse(currentRun))
      if (url.pathname === '/api/runs/run-chat-b/operations') {
        return Promise.resolve(jsonResponse({
          run_id: currentRun.run_id,
          operations: [],
          next_sequence: 0,
          has_more: false,
          conversation_id: currentRun.conversation_id,
        }))
      }
      if (url.pathname === '/api/runs/run-chat-b/recovery') {
        return Promise.resolve(jsonResponse({
          run_id: currentRun.run_id,
          run: currentRun,
          unresolved_operations: [],
          operations_next_sequence: 0,
          operations_truncated: false,
        }))
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result, rerender } = renderHook(({ conversationId }) => useRunController(conversationId), {
      initialProps: { conversationId: 'chat-a' },
    })
    await waitFor(() => expect(chatASignal).toBeDefined())
    rerender({ conversationId: 'chat-b' })
    await waitFor(() => expect(result.current.run?.run_id).toBe('run-chat-b'))
    expect(chatASignal?.aborted).toBe(true)

    await act(async () => {
      resolveChatA(jsonResponse({ runs: [staleRun], next_run_id: null, has_more: false }))
    })

    expect(result.current.runs.map((item) => item.run_id)).toEqual(['run-chat-b'])
    expect(result.current.snapshot?.conversation_id).toBe('chat-b')
  })

  it('fails closed without a conversation and never requests the account-wide Run list', async () => {
    installDesktopSession()
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useRunController(null))
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(async () => { await result.current.refreshLatest() })

    expect(result.current.runs).toEqual([])
    expect(result.current.snapshot).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('refreshLatest selects and loads a newly created Run from the actual route page shape', async () => {
    installDesktopSession()
    const firstRun = run('run-existing', 'chat-latest')
    const newestRun: RunSnapshot = {
      ...run('run-created-later', 'chat-latest'),
      revision: 2,
      last_seq: 2,
      created_at: '2026-11-03T02:00:00Z',
      updated_at: '2026-11-03T02:01:00Z',
      finished_at: '2026-11-03T02:01:00Z',
    }
    let routeRuns = [firstRun]
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/runs') {
        return jsonResponse({ runs: routeRuns, next_run_id: null, has_more: false })
      }
      const runId = url.pathname.match(/^\/api\/runs\/([^/]+)$/)?.[1]
      const selected = routeRuns.find((item) => item.run_id === runId)
      if (selected) return jsonResponse(selected)
      if (url.pathname.endsWith('/operations')) {
        const selectedRunId = url.pathname.split('/')[3]
        const selectedRun = routeRuns.find((item) => item.run_id === selectedRunId)!
        return jsonResponse({
          run_id: selectedRun.run_id, operations: [], next_sequence: 0, has_more: false,
          conversation_id: selectedRun.conversation_id,
        })
      }
      if (url.pathname.endsWith('/recovery')) {
        const selectedRunId = url.pathname.split('/')[3]
        const selectedRun = routeRuns.find((item) => item.run_id === selectedRunId)!
        return jsonResponse({
          run_id: selectedRun.run_id, run: selectedRun, unresolved_operations: [],
          operations_next_sequence: 0, operations_truncated: false,
        })
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useRunController('chat-latest'))
    await waitFor(() => expect(result.current.run?.run_id).toBe('run-existing'))

    routeRuns = [firstRun, newestRun]
    await act(async () => { await result.current.refreshLatest() })

    expect(result.current.runs.map((item) => item.run_id)).toEqual(['run-created-later', 'run-existing'])
    expect(result.current.selectedRunId).toBe('run-created-later')
    expect(result.current.snapshot).toMatchObject({ run_id: 'run-created-later', revision: 2 })
  })

  it('keeps the newer event snapshot and detail bundle when an older detail request resolves late', async () => {
    installDesktopSession()
    const runId = 'run-detail-revision-fence'
    const conversationId = 'chat-detail-revision-fence'
    const initialSnapshot = runAtRevision(runId, conversationId, 4, 4)
    const staleSnapshot = runAtRevision(runId, conversationId, 5, 5)
    const latestSnapshot = runAtRevision(runId, conversationId, 8, 5, 'paused')
    const initialOperation = operation(runId, 'op-revision-4', 4)
    const staleOperation = operation(runId, 'op-revision-5', 5)
    const latestOperation = operation(runId, 'op-revision-8', 8)
    const stream = controlledEventStream()
    let serverSnapshot = initialSnapshot
    let serverOperation = initialOperation
    let runReads = 0
    let operationReads = 0
    let recoveryReads = 0
    let pendingStaleParts = 0
    let markStaleBundleStarted!: () => void
    const staleBundleStarted = new Promise<void>((resolve) => { markStaleBundleStarted = resolve })
    let resolveStaleRun!: (response: Response) => void
    let resolveStaleOperations!: (response: Response) => void
    let resolveStaleRecovery!: (response: Response) => void
    const stalePartStarted = () => {
      pendingStaleParts += 1
      if (pendingStaleParts === 3) markStaleBundleStarted()
    }
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/runs') {
        return Promise.resolve(jsonResponse({ runs: [serverSnapshot], next_run_id: null, has_more: false }))
      }
      if (url.pathname === `/api/runs/${runId}/events`) {
        return Promise.resolve(jsonResponse(eventPage(runId, initialSnapshot.last_seq)))
      }
      if (url.pathname === `/api/runs/${runId}/events/stream`) return Promise.resolve(stream.response)
      if (url.pathname === `/api/runs/${runId}`) {
        runReads += 1
        if (runReads === 2) {
          return new Promise<Response>((resolve) => {
            resolveStaleRun = resolve
            stalePartStarted()
          })
        }
        return Promise.resolve(jsonResponse(serverSnapshot))
      }
      if (url.pathname === `/api/runs/${runId}/operations`) {
        operationReads += 1
        if (operationReads === 2) {
          return new Promise<Response>((resolve) => {
            resolveStaleOperations = resolve
            stalePartStarted()
          })
        }
        return Promise.resolve(jsonResponse(operationPage(serverSnapshot, serverOperation)))
      }
      if (url.pathname === `/api/runs/${runId}/recovery`) {
        recoveryReads += 1
        if (recoveryReads === 2) {
          return new Promise<Response>((resolve) => {
            resolveStaleRecovery = resolve
            stalePartStarted()
          })
        }
        return Promise.resolve(jsonResponse(recoveryReport(serverSnapshot, serverOperation)))
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useRunController(conversationId))
    try {
      await waitFor(() => {
        expect(result.current.snapshot?.revision).toBe(4)
        expect(result.current.eventsConnected).toBe(true)
      })

      let staleRefresh!: Promise<void>
      act(() => { staleRefresh = result.current.refreshRun() })
      await staleBundleStarted

      serverSnapshot = latestSnapshot
      serverOperation = latestOperation
      await act(async () => {
        stream.publish({
          run_id: runId,
          seq: 5,
          type: 'run.updated',
          at: latestSnapshot.updated_at,
          payload: { revision: 8, status: 'paused' },
        })
      })
      await waitFor(() => {
        expect(result.current.snapshot).toMatchObject({ revision: 8, status: 'paused' })
        expect(result.current.operations.map((item) => item.operation_id)).toEqual(['op-revision-8'])
        expect(result.current.recovery?.run.revision).toBe(8)
      })

      await act(async () => {
        resolveStaleRun(jsonResponse(staleSnapshot))
        resolveStaleOperations(jsonResponse(operationPage(staleSnapshot, staleOperation)))
        resolveStaleRecovery(jsonResponse(recoveryReport(staleSnapshot, staleOperation)))
        await staleRefresh
      })

      expect(result.current.snapshot).toMatchObject({ revision: 8, status: 'paused' })
      expect(result.current.operations.map((item) => item.operation_id)).toEqual(['op-revision-8'])
      expect(result.current.recovery).toMatchObject({
        run: { revision: 8, status: 'paused' },
        unresolved_operations: [{ operation_id: 'op-revision-8' }],
      })
    } finally {
      cleanup()
      stream.close()
    }
  })

  it('does not roll back an event-updated snapshot when an older pause response arrives', async () => {
    installDesktopSession()
    const runId = 'run-control-revision-fence'
    const conversationId = 'chat-control-revision-fence'
    const initialSnapshot = runAtRevision(runId, conversationId, 4, 4)
    const staleSnapshot = runAtRevision(runId, conversationId, 5, 5, 'pausing')
    const latestSnapshot = runAtRevision(runId, conversationId, 8, 5, 'paused')
    const latestOperation = operation(runId, 'op-control-revision-8', 8)
    const stream = controlledEventStream()
    let serverSnapshot = initialSnapshot
    let serverOperation = operation(runId, 'op-control-revision-4', 4)
    let listReads = 0
    let resolvePause!: (response: Response) => void
    let resolveRefreshList!: (response: Response) => void
    let markRefreshListStarted!: () => void
    const refreshListStarted = new Promise<void>((resolve) => { markRefreshListStarted = resolve })
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input))
      if (url.pathname === '/api/runs') {
        listReads += 1
        if (listReads === 2) {
          return new Promise<Response>((resolve) => {
            resolveRefreshList = resolve
            markRefreshListStarted()
          })
        }
        return Promise.resolve(jsonResponse({ runs: [serverSnapshot], next_run_id: null, has_more: false }))
      }
      if (url.pathname === `/api/runs/${runId}/pause`) {
        return new Promise<Response>((resolve) => { resolvePause = resolve })
      }
      if (url.pathname === `/api/runs/${runId}/events`) {
        return Promise.resolve(jsonResponse(eventPage(runId, initialSnapshot.last_seq)))
      }
      if (url.pathname === `/api/runs/${runId}/events/stream`) return Promise.resolve(stream.response)
      if (url.pathname === `/api/runs/${runId}`) return Promise.resolve(jsonResponse(serverSnapshot))
      if (url.pathname === `/api/runs/${runId}/operations`) {
        return Promise.resolve(jsonResponse(operationPage(serverSnapshot, serverOperation)))
      }
      if (url.pathname === `/api/runs/${runId}/recovery`) {
        return Promise.resolve(jsonResponse(recoveryReport(serverSnapshot, serverOperation)))
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useRunController(conversationId))
    try {
      await waitFor(() => {
        expect(result.current.snapshot?.revision).toBe(4)
        expect(result.current.eventsConnected).toBe(true)
      })

      let pausePromise!: Promise<unknown>
      act(() => { pausePromise = result.current.pause() })
      await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/api/runs/${runId}/pause`),
        expect.objectContaining({ method: 'POST' }),
      ))

      serverSnapshot = latestSnapshot
      serverOperation = latestOperation
      await act(async () => {
        stream.publish({
          run_id: runId,
          seq: 5,
          type: 'run.updated',
          at: latestSnapshot.updated_at,
          payload: { revision: 8, status: 'paused' },
        })
      })
      await waitFor(() => {
        expect(result.current.snapshot).toMatchObject({ revision: 8, status: 'paused' })
        expect(result.current.operations.map((item) => item.operation_id)).toEqual(['op-control-revision-8'])
        expect(result.current.recovery?.run.revision).toBe(8)
      })

      await act(async () => {
        resolvePause(jsonResponse({ accepted: true, run: staleSnapshot }))
        await Promise.resolve()
      })
      await refreshListStarted

      expect(result.current.snapshot).toMatchObject({ revision: 8, status: 'paused' })
      expect(result.current.operations.map((item) => item.operation_id)).toEqual(['op-control-revision-8'])
      expect(result.current.recovery?.run.revision).toBe(8)

      await act(async () => {
        resolveRefreshList(jsonResponse({ runs: [latestSnapshot], next_run_id: null, has_more: false }))
        await pausePromise
      })
      expect(result.current.snapshot).toMatchObject({ revision: 8, status: 'paused' })
      expect(result.current.operations.map((item) => item.operation_id)).toEqual(['op-control-revision-8'])
      expect(result.current.recovery?.run.revision).toBe(8)
    } finally {
      cleanup()
      stream.close()
    }
  })
  for (const action of ['pause', 'resume'] as const) {
    it(`${action === 'pause' ? 'retries Pause once' : 'does not retry Continue'} after a revision conflict from a Run that moved on`, async () => {
      installDesktopSession()
      const runId = `run-conflict-${action}`
      const conversationId = `chat-conflict-${action}`
      const status = action === 'pause' ? 'running' : 'paused'
      let serverSnapshot = runAtRevision(runId, conversationId, 4, 4, status)
      const commandBodies: number[] = []
      const stream = controlledEventStream()
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input))
        if (url.pathname === '/api/runs') {
          return Promise.resolve(jsonResponse({ runs: [serverSnapshot], next_run_id: null, has_more: false }))
        }
        if (url.pathname === `/api/runs/${runId}/${action}`) {
          const expected = JSON.parse(String(init?.body)).expected_revision as number
          commandBodies.push(expected)
          if (expected !== serverSnapshot.revision) {
            return Promise.resolve(new Response(JSON.stringify({ code: 'revision_conflict', message: 'The Run changed since the client last read it.' }), {
              status: 409, headers: { 'Content-Type': 'application/json' },
            }))
          }
          return Promise.resolve(jsonResponse({ accepted: true, run: runAtRevision(runId, conversationId, expected + 1, expected + 1, action === 'pause' ? 'pausing' : 'running') }))
        }
        if (url.pathname === `/api/runs/${runId}/events`) return Promise.resolve(jsonResponse(eventPage(runId, 4)))
        if (url.pathname === `/api/runs/${runId}/events/stream`) return Promise.resolve(stream.response)
        if (url.pathname === `/api/runs/${runId}`) return Promise.resolve(jsonResponse(serverSnapshot))
        if (url.pathname === `/api/runs/${runId}/operations`) {
          return Promise.resolve(jsonResponse(operationPage(serverSnapshot, operation(runId, 'op-1', 1))))
        }
        if (url.pathname === `/api/runs/${runId}/recovery`) {
          return Promise.resolve(jsonResponse(recoveryReport(serverSnapshot, operation(runId, 'op-1', 1))))
        }
        throw new Error(`Unexpected request: ${url}`)
      })
      vi.stubGlobal('fetch', fetchMock)

      const { result } = renderHook(() => useRunController(conversationId))
      try {
        await waitFor(() => expect(result.current.snapshot?.revision).toBe(4))
        // The Run advances (for example an operation is verified) before the UI hears about it.
        serverSnapshot = runAtRevision(runId, conversationId, 6, 6, status)
        let outcome: unknown
        await act(async () => {
          outcome = await result.current[action]().catch((error: unknown) => error)
        })
        if (action === 'pause') {
          expect(commandBodies).toEqual([4, 6])
          expect(outcome).toMatchObject({ accepted: true })
        } else {
          expect(commandBodies).toEqual([4])
          expect(outcome).toMatchObject({ code: 'revision_conflict' })
          await waitFor(() => expect(result.current.snapshot?.revision).toBe(6))
        }
      } finally {
        cleanup()
        stream.close()
      }
    })
  }
})
