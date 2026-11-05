import { apiFetch, BASE, decodeApiError, JSON_HEADERS, throwApiError } from './client'
import { getPersistentIdempotencyKey } from './idempotency'

export type RunStatus =
  | 'running'
  | 'waiting_approval'
  | 'pausing'
  | 'paused'
  | 'interrupted'
  | 'needs_review'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type RunAction = 'inspect' | 'pause' | 'resume' | 'stop' | 'approve' | 'reject' | 'verify' | 'skip'
export type OperationStatus =
  | 'prepared'
  | 'authorized'
  | 'executing'
  | 'verified'
  | 'failed'
  | 'cancelled'
  | 'uncertain'
  | 'needs_review'
  | 'returned'
export type OperationEffectClass = 'read_only' | 'verifiable_write' | 'opaque_effect'

export interface RunSnapshot {
  run_id: string
  conversation_id: string
  attempt: number
  revision: number
  status: RunStatus
  reason_code: string | null
  available_actions: RunAction[]
  last_seq: number
  created_at: string
  updated_at: string
  finished_at: string | null
  assistant_message_id?: number | null
}

export interface RunOperation {
  run_id: string
  operation_id: string
  sequence: number
  attempt: number
  operation_kind: string
  related_operation_id: string | null
  effect_class: OperationEffectClass
  status: OperationStatus
  reason_code: string | null
  input_summary: Record<string, string | number | boolean>
  evidence: Record<string, string | number | boolean>
  /** Evidence is not a guarantee that Undo is currently safe. */
  snapshot_available: boolean
  created_at: string
  updated_at: string
}

export interface RunEvent {
  run_id: string
  seq: number
  type: string
  at: string
  payload: Record<string, unknown>
}

export interface RunEventsPage {
  run_id: string
  events: RunEvent[]
  next_seq: number
  last_seq: number
}

export interface RunOperationsPage {
  run_id: string
  operations: RunOperation[]
  next_sequence: number
  has_more: boolean
  conversation_id: string
}

export interface RunsPage {
  runs: RunSnapshot[]
  next_run_id: string | null
  has_more: boolean
}

export interface RecoveryReport {
  run_id: string
  run: RunSnapshot
  unresolved_operations: RunOperation[]
  operations_next_sequence: number
  operations_truncated: boolean
}

export interface RunCommandResponse {
  accepted: boolean
  run: RunSnapshot
}

export interface RunOperationResolveResponse {
  accepted: boolean
  verification_result: 'completed' | 'unchanged' | 'unknown' | string
  run: RunSnapshot
  operation: RunOperation
}

export type OperationResolution = 'verify' | 'skip'

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) throw await decodeApiError(response, fallback)
  return response.json() as Promise<T>
}

function runPath(runId: string): string {
  return `${BASE}/api/runs/${encodeURIComponent(runId)}`
}

export interface FetchRunsOptions {
  limit?: number
  afterRunId?: string
  signal?: AbortSignal
}

export async function fetchRuns(
  conversationId?: string | null,
  options: FetchRunsOptions = {},
): Promise<RunsPage> {
  const params = new URLSearchParams()
  if (conversationId) params.set('conversation_id', conversationId)
  if (options.limit !== undefined) params.set('limit', String(options.limit))
  if (options.afterRunId) params.set('after_run_id', options.afterRunId)
  const query = params.size ? `?${params.toString()}` : ''
  const response = await apiFetch(`${BASE}/api/runs${query}`, { signal: options.signal })
  return readJson<RunsPage>(response, 'Failed to fetch Runs')
}

export async function fetchRun(runId: string, signal?: AbortSignal): Promise<RunSnapshot> {
  const response = await apiFetch(runPath(runId), { signal })
  return readJson<RunSnapshot>(response, 'Failed to fetch Run')
}

export async function fetchRunOperations(
  runId: string,
  afterSequence = 0,
  limit = 100,
  signal?: AbortSignal,
): Promise<RunOperationsPage> {
  const params = new URLSearchParams({ after_sequence: String(afterSequence), limit: String(limit) })
  const response = await apiFetch(`${runPath(runId)}/operations?${params}`, { signal })
  return readJson<RunOperationsPage>(response, 'Failed to fetch Run operations')
}

export async function fetchRunRecovery(runId: string, signal?: AbortSignal): Promise<RecoveryReport> {
  const response = await apiFetch(`${runPath(runId)}/recovery`, { signal })
  return readJson<RecoveryReport>(response, 'Failed to fetch Run recovery report')
}

export async function fetchRunEvents(
  runId: string,
  afterSeq = 0,
  limit = 100,
  signal?: AbortSignal,
): Promise<RunEventsPage> {
  const params = new URLSearchParams({ after_seq: String(afterSeq), limit: String(limit) })
  const response = await apiFetch(`${runPath(runId)}/events?${params}`, { signal })
  return readJson<RunEventsPage>(response, 'Failed to fetch Run events')
}

export class RunEventStreamResetError extends Error {
  code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'RunEventStreamResetError'
    this.code = code
  }
}

export interface StreamRunEventsOptions {
  signal?: AbortSignal
  onOpen?: () => void
}

function parseSsePayload(data: string): unknown {
  try {
    return JSON.parse(data)
  } catch {
    return null
  }
}

/** Authenticated fetch-based SSE; native EventSource cannot attach the desktop bearer token. */
export async function streamRunEvents(
  runId: string,
  afterSeq: number,
  onEvent: (event: RunEvent) => void | Promise<void>,
  options: StreamRunEventsOptions = {},
): Promise<void> {
  const params = new URLSearchParams({ after_seq: String(afterSeq) })
  const response = await apiFetch(`${runPath(runId)}/events/stream?${params}`, {
    headers: { Accept: 'text/event-stream' },
    signal: options.signal,
  })
  if (!response.ok) await throwApiError(response, 'Failed to connect to Run event stream')
  if (!response.body) throw new Error('Run event stream did not provide a readable response body')

  options.onOpen?.()
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventName = ''
  let dataLines: string[] = []

  const dispatch = async () => {
    if (dataLines.length === 0) {
      eventName = ''
      return
    }
    const data = parseSsePayload(dataLines.join('\n'))
    const name = eventName
    eventName = ''
    dataLines = []
    if (name === 'reset') {
      const record = data && typeof data === 'object' ? data as Record<string, unknown> : {}
      throw new RunEventStreamResetError(
        typeof record.code === 'string' ? record.code : 'stream_reset',
        typeof record.message === 'string' ? record.message : 'Run event stream requested a reset',
      )
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) return
    await onEvent(data as RunEvent)
  }

  const consumeLines = async (final = false) => {
    const lines = buffer.split(/\r?\n/)
    buffer = final ? '' : lines.pop() ?? ''
    for (const line of lines) {
      if (line === '') {
        await dispatch()
      } else if (line.startsWith(':')) {
        continue
      } else {
        const separator = line.indexOf(':')
        const field = separator < 0 ? line : line.slice(0, separator)
        const rawValue = separator < 0 ? '' : line.slice(separator + 1)
        const value = rawValue.startsWith(' ') ? rawValue.slice(1) : rawValue
        if (field === 'event') eventName = value
        if (field === 'data') dataLines.push(value)
      }
    }
    if (final) await dispatch()
  }

  try {
    while (!options.signal?.aborted) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      await consumeLines()
    }
    buffer += decoder.decode()
    await consumeLines(true)
  } finally {
    try {
      await reader.cancel()
    } catch {
      // The stream may already have closed or been aborted.
    }
  }
}

export type RunCommand = 'pause' | 'resume' | 'stop'

export function createRunIdempotencyKey(
  action: RunCommand | 'resolve',
  runId: string,
  expectedRevision: number,
  expectedAttempt: number,
  operationId?: string,
  resolution?: OperationResolution,
): string {
  return getPersistentIdempotencyKey([
    'run-command', action, runId, expectedRevision, expectedAttempt, operationId ?? '', resolution ?? '',
  ])
}

async function postRunCommand(
  action: RunCommand,
  runId: string,
  snapshot: Pick<RunSnapshot, 'revision' | 'attempt'>,
  idempotencyKey?: string,
): Promise<RunCommandResponse> {
  const body = { expected_revision: snapshot.revision, expected_attempt: snapshot.attempt }
  const key = idempotencyKey ?? createRunIdempotencyKey(
    action, runId, snapshot.revision, snapshot.attempt,
  )
  const headers = new Headers(JSON_HEADERS)
  headers.set('Idempotency-Key', key)
  const response = await apiFetch(`${runPath(runId)}/${action}`, {
    method: 'POST', headers, body: JSON.stringify(body),
  })
  return readJson<RunCommandResponse>(response, `Failed to ${action} Run`)
}

export const pauseRun = (
  runId: string,
  snapshot: Pick<RunSnapshot, 'revision' | 'attempt'>,
  idempotencyKey?: string,
) => postRunCommand('pause', runId, snapshot, idempotencyKey)

export const resumeRun = (
  runId: string,
  snapshot: Pick<RunSnapshot, 'revision' | 'attempt'>,
  idempotencyKey?: string,
) => postRunCommand('resume', runId, snapshot, idempotencyKey)

export const stopRun = (
  runId: string,
  snapshot: Pick<RunSnapshot, 'revision' | 'attempt'>,
  idempotencyKey?: string,
) => postRunCommand('stop', runId, snapshot, idempotencyKey)

export function createResolveIdempotencyKey(
  runId: string,
  operationId: string,
  resolution: OperationResolution,
  snapshot: Pick<RunSnapshot, 'revision' | 'attempt'>,
): string {
  return createRunIdempotencyKey(
    'resolve', runId, snapshot.revision, snapshot.attempt, operationId, resolution,
  )
}

export async function resolveRunOperation(
  runId: string,
  operationId: string,
  resolution: OperationResolution,
  snapshot: Pick<RunSnapshot, 'revision' | 'attempt'>,
  idempotencyKey?: string,
): Promise<RunOperationResolveResponse> {
  const body = {
    resolution,
    expected_revision: snapshot.revision,
    expected_attempt: snapshot.attempt,
  }
  const key = idempotencyKey ?? createResolveIdempotencyKey(runId, operationId, resolution, snapshot)
  const headers = new Headers(JSON_HEADERS)
  headers.set('Idempotency-Key', key)
  const response = await apiFetch(`${runPath(runId)}/operations/${encodeURIComponent(operationId)}/resolve`, {
    method: 'POST', headers, body: JSON.stringify(body),
  })
  return readJson<RunOperationResolveResponse>(response, `Failed to ${resolution} operation`)
}
