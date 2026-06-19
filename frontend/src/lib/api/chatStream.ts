import { apiFetch, BASE, JSON_HEADERS } from './client'
import type {
  AccessGrantRequiredEvent,
  ApprovalEvent,
  PlanEvent,
  StepEvent,
  UploadedAttachment,
} from './types'

const DEBUG_ENDPOINT = String(import.meta.env.VITE_DEBUG_INGEST_URL || '').trim()
const DEBUG_SESSION_ID = DEBUG_ENDPOINT
  ? String(import.meta.env.VITE_DEBUG_SESSION_ID || '').trim()
    || (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID().slice(0, 8)
      : `debug-${Date.now().toString(36)}`)
  : ''
const DEBUG_HEADERS = {
  'Content-Type': 'application/json',
  ...(DEBUG_SESSION_ID ? { 'X-Debug-Session-Id': DEBUG_SESSION_ID } : {}),
}

interface ChatStreamHandlers {
  onToken: (token: string) => void
  onToolStart: (data: { tool: string; input: unknown; call_id?: string }) => void
  onToolEnd: (data: { output: string; call_id?: string }) => void
  onDone: (data: DoneEvent) => void
  onError: (msg: string, data?: StreamErrorEvent) => void
  onApprovalRequired?: (data: ApprovalEvent) => void
  onApprovalResolved?: (data: ApprovalEvent) => void
  onExecutionResumed?: (data: ApprovalEvent) => void
  onAccessGrantRequired?: (data: AccessGrantRequiredEvent) => void
  onToolResumed?: (data: { tool: string; output: string }) => void
  onPing?: () => void
  onHeartbeat?: () => void
  onActivity?: () => void
  onThinking?: (content: string) => void
  onProgress?: (content: string) => void
  onPlan?: (plan: PlanEvent) => void
  onStepStart?: (data: StepEvent) => void
  onStepComplete?: (data: StepEvent) => void
  onObservation?: (data: StepEvent) => void
  onRetry?: (data: StepEvent) => void
}

interface DoneEvent {
  conversation_id?: string
  summary?: string
  status?: 'complete' | 'paused' | 'error'
  incomplete?: boolean
  reason_code?: string
  attachments?: UploadedAttachment[]
  response_duration_ms?: number | null
}

interface StreamErrorEvent {
  code?: string
  message?: string
}

interface ChatStreamState {
  evtCounts: Record<string, number>
  sawDone: boolean
  sawTerminal: boolean
}

function sendDebugEvent(
  message: string,
  hypothesisId: string,
  data: Record<string, unknown>
) {
  if (!DEBUG_ENDPOINT) return
  fetch(DEBUG_ENDPOINT, {
    method: 'POST',
    headers: DEBUG_HEADERS,
    body: JSON.stringify({
      sessionId: DEBUG_SESSION_ID || undefined,
      hypothesisId,
      location: 'lib/api/chatStream',
      message,
      data,
      timestamp: Date.now(),
    }),
  }).catch(() => {})
}

function bumpEvt(state: ChatStreamState, key: string) {
  state.evtCounts[key] = (state.evtCounts[key] ?? 0) + 1
}

function dispatchSseEvent(
  eventType: string,
  raw: string,
  state: ChatStreamState,
  handlers: ChatStreamHandlers
) {
  try {
    const json = JSON.parse(raw)
    if (eventType === 'token') {
      bumpEvt(state, 'token')
      handlers.onToken(json.content)
    } else if (eventType === 'tool_start') {
      bumpEvt(state, 'tool_start')
      handlers.onToolStart(json)
    } else if (eventType === 'tool_end') {
      bumpEvt(state, 'tool_end')
      handlers.onToolEnd(json)
    } else if (eventType === 'done') {
      bumpEvt(state, 'done')
      state.sawDone = true
      state.sawTerminal = true
      handlers.onDone(json)
    } else if (eventType === 'error') {
      bumpEvt(state, 'error')
      state.sawTerminal = true
      handlers.onError(json.message, json)
    } else if (eventType === 'approval_required') {
      bumpEvt(state, 'approval_required')
      handlers.onApprovalRequired?.(json)
    } else if (eventType === 'approval_resolved') {
      bumpEvt(state, 'approval_resolved')
      handlers.onApprovalResolved?.(json)
    } else if (eventType === 'execution_resumed') {
      bumpEvt(state, 'execution_resumed')
      handlers.onExecutionResumed?.(json)
    } else if (eventType === 'thinking') {
      bumpEvt(state, 'thinking')
      handlers.onThinking?.(json.content)
    } else if (eventType === 'progress') {
      bumpEvt(state, 'progress')
      handlers.onProgress?.(json.content)
    } else if (eventType === 'plan') {
      bumpEvt(state, 'plan')
      handlers.onPlan?.(json)
    } else if (eventType === 'step_start') {
      bumpEvt(state, 'step_start')
      handlers.onStepStart?.(json)
    } else if (eventType === 'step_complete') {
      bumpEvt(state, 'step_complete')
      handlers.onStepComplete?.(json)
    } else if (eventType === 'observation') {
      bumpEvt(state, 'observation')
      handlers.onObservation?.(json)
    } else if (eventType === 'retry') {
      bumpEvt(state, 'retry')
      handlers.onRetry?.(json)
    } else if (eventType === 'access_grant_required') {
      bumpEvt(state, 'access_grant_required')
      handlers.onAccessGrantRequired?.(json)
    } else if (eventType === 'tool_resumed') {
      bumpEvt(state, 'tool_resumed')
      handlers.onToolResumed?.(json)
    } else if (eventType === 'ping') {
      handlers.onPing?.()
    } else if (eventType === 'heartbeat') {
      bumpEvt(state, 'heartbeat')
      handlers.onHeartbeat?.()
    }
  } catch {
    bumpEvt(state, 'json_parse_error')
    sendDebugEvent('json_parse_error', 'H4', {
      eventType,
      rawTail: raw.slice(-200),
    })
  }
}

function processSseFrame(
  frame: string,
  state: ChatStreamState,
  handlers: ChatStreamHandlers
) {
  if (!frame.trim()) return

  let eventType = ''
  const dataLines: string[] = []

  for (const rawLine of frame.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (line.startsWith('event: ')) {
      eventType = line.slice(7).trim()
    } else if (line.startsWith('data: ')) {
      dataLines.push(line.slice(6))
    }
  }

  if (dataLines.length === 0) return
  dispatchSseEvent(eventType, dataLines.join('\n'), state, handlers)
}

function processSseBuffer(
  buffer: string,
  flush: boolean,
  state: ChatStreamState,
  handlers: ChatStreamHandlers
) {
  let remaining = buffer.replace(/\r\n/g, '\n')

  while (true) {
    const frameEnd = remaining.indexOf('\n\n')
    if (frameEnd === -1) break
    const frame = remaining.slice(0, frameEnd)
    remaining = remaining.slice(frameEnd + 2)
    processSseFrame(frame, state, handlers)
  }

  if (flush && remaining.trim()) {
    processSseFrame(remaining, state, handlers)
    return ''
  }

  return remaining
}

export function chatStream(
  message: string,
  conversationId: string | null,
  attachments: UploadedAttachment[],
  onToken: (token: string) => void,
  onToolStart: (data: { tool: string; input: unknown; call_id?: string }) => void,
  onToolEnd: (data: { output: string; call_id?: string }) => void,
  onDone: (data: DoneEvent) => void,
  onError: (msg: string, data?: StreamErrorEvent) => void,
  onApprovalRequired?: (data: ApprovalEvent) => void,
  onApprovalResolved?: (data: ApprovalEvent) => void,
  onExecutionResumed?: (data: ApprovalEvent) => void,
  onThinking?: (content: string) => void,
  onProgress?: (content: string) => void,
  onPlan?: (plan: PlanEvent) => void,
  onStepStart?: (data: StepEvent) => void,
  onStepComplete?: (data: StepEvent) => void,
  onObservation?: (data: StepEvent) => void,
  onRetry?: (data: StepEvent) => void,
  onAccessGrantRequired?: (data: AccessGrantRequiredEvent) => void,
  onToolResumed?: (data: { tool: string; output: string }) => void,
  onPing?: () => void,
  onHeartbeat?: () => void,
  onActivity?: () => void,
): () => void {
  const controller = new AbortController()

  ;(async () => {
    const state: ChatStreamState = {
      evtCounts: {},
      sawDone: false,
      sawTerminal: false,
    }

    const handlers: ChatStreamHandlers = {
      onToken,
      onToolStart,
      onToolEnd,
      onDone,
      onError,
      onApprovalRequired,
      onApprovalResolved,
      onExecutionResumed,
      onAccessGrantRequired,
      onToolResumed,
      onPing,
      onHeartbeat,
      onActivity,
      onThinking,
      onProgress,
      onPlan,
      onStepStart,
      onStepComplete,
      onObservation,
      onRetry,
    }

    try {
      const res = await apiFetch(`${BASE}/api/chat`, {
        method: 'POST',
        headers: JSON_HEADERS,
        body: JSON.stringify({ message, conversation_id: conversationId, attachments }),
        signal: controller.signal,
      })

      sendDebugEvent('fetch_response', 'H2', {
        ok: res.ok,
        status: res.status,
        hasBody: !!res.body,
      })

      if (!res.ok || !res.body) {
        onError('Request failed')
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          buffer += decoder.decode()
          buffer = processSseBuffer(buffer, true, state, handlers)
          sendDebugEvent('final_buffer_flushed', 'H3', {
            bufferLenAfterFlush: buffer.length,
            sawDone: state.sawDone,
            sawTerminal: state.sawTerminal,
            evtCounts: state.evtCounts,
          })

          if (!state.sawTerminal) {
            bumpEvt(state, 'fallback_done')
            sendDebugEvent('stream_closed_without_terminal_event', 'H3', {
              conversationId,
              evtCounts: state.evtCounts,
            })
            handlers.onDone({ conversation_id: conversationId ?? '' })
            state.sawDone = true
            state.sawTerminal = true
          }
          break
        }

        if (value && value.length > 0) {
          handlers.onActivity?.()
        }
        buffer += decoder.decode(value, { stream: true })
        buffer = processSseBuffer(buffer, false, state, handlers)
      }
    } catch (err: any) {
      sendDebugEvent('catch', 'H2', {
        name: err?.name,
        errMsg: String(err?.message ?? err).slice(0, 200),
      })
      if (err.name !== 'AbortError') onError(err.message ?? 'Stream error')
    }
  })()

  return () => controller.abort()
}
