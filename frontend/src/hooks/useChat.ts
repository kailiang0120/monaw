import { useState, useCallback, useRef, useEffect } from 'react'
import { chatStream } from '../lib/api/chatStream'
import { fetchMessages } from '../lib/api/conversations'
import {
  type ApprovalEvent,
  type PlanEvent,
  type PlanStep,
  type StepEvent,
  type AccessGrantRequiredEvent,
  type SavedMessage,
} from '../lib/api/types'
import type { UploadedAttachment } from '../lib/api/types'

/** After this many ms with no transport activity while streaming, auto-stop. */
const IDLE_TIMEOUT_MS = 600_000
const RUN_STATUSES = new Set(['streaming', 'complete', 'paused', 'error'])
const TYPEWRITER_INTERVAL_MS = 24
const TYPEWRITER_CHARS_PER_TICK = 18
const TYPEWRITER_FAST_BACKLOG_CHARS = 900
const TYPEWRITER_MAX_CHARS_PER_TICK = 72
const INITIAL_HISTORY_LIMIT = 5
const OLDER_HISTORY_LIMIT = 30
const HISTORY_LOAD_TIMEOUT_MS = 10_000

export interface ToolCall {
  id?: string
  tool: string
  input: string
  output?: string
  pending?: boolean
  status?: string
  previewOnly?: boolean
}

export type ActivityItem =
  | { id: string; type: 'progress'; content: string }
  | { id: string; type: 'tool'; toolCall: ToolCall }

export interface ApprovalNotice {
  ticket_id: string
  action: string
  reason: string
  type: 'required' | 'resolved' | 'resumed'
}

export interface StepProgress {
  step_id: string
  description: string
  status: 'pending' | 'active' | 'done' | 'failed' | 'retrying'
  observation?: string
  retry_count?: number
}

export interface AccessGrantNotice {
  ticket_id: string
  target_type: string
  target_identifier: string
  display_name: string
  action_context: string
  requested_access?: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  attachments?: UploadedAttachment[]
  toolCalls?: ToolCall[]
  approvals?: ApprovalNotice[]
  streaming?: boolean
  thinking?: string
  activityItems?: ActivityItem[]
  plan?: PlanStep[]
  stepProgress?: StepProgress[]
  runStatus?: 'streaming' | 'complete' | 'paused' | 'error'
  responseStartedAtMs?: number
  responseDurationMs?: number
  contentRevision?: number
}

interface StreamDonePayload {
  conversation_id?: string
  summary?: string
  status?: string
  incomplete?: boolean
  attachments?: UploadedAttachment[]
  response_duration_ms?: number | null
}

function normalizeRunStatus(status?: string): Message['runStatus'] | undefined {
  return status && RUN_STATUSES.has(status) ? (status as Message['runStatus']) : undefined
}

function savedMessageToMessage(m: SavedMessage): Message {
  return {
    id: String(m.id),
    role: m.role as 'user' | 'assistant',
    content: m.content,
    attachments: m.attachments?.length ? m.attachments : undefined,
    thinking: m.thinking || undefined,
    runStatus: m.role === 'assistant' ? normalizeRunStatus(m.status) : undefined,
    responseDurationMs: m.response_duration_ms ?? undefined,
    toolCalls: m.tool_calls.length
      ? m.tool_calls.map((tc) => ({
          id: String(tc.id),
          tool: tc.tool_name,
          input: tc.input,
          output: tc.output,
          pending: false,
          status: tc.status,
          previewOnly: tc.preview_only ?? false,
        }))
      : undefined,
  }
}

export function useChat(conversationId: string | null) {
  const [messages, setMessages] = useState<Message[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [isLoadingOlderHistory, setIsLoadingOlderHistory] = useState(false)
  const [hasMoreHistory, setHasMoreHistory] = useState(false)
  const [pendingAccessGrant, setPendingAccessGrant] = useState<AccessGrantNotice | null>(null)
  const [refreshNonce, setRefreshNonce] = useState(0)
  const abortRef = useRef<(() => void) | null>(null)
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const typewriterCleanupRef = useRef<(() => void) | null>(null)
  const conversationIdRef = useRef<string | null>(conversationId)
  const historyCursorRef = useRef<number | null>(null)

  useEffect(() => {
    conversationIdRef.current = conversationId
  }, [conversationId])

  // Load saved messages when conversationId changes
  useEffect(() => {
    if (!conversationId) {
      if (!isStreaming) setMessages([])
      setHasMoreHistory(false)
      setIsLoadingOlderHistory(false)
      historyCursorRef.current = null
      return
    }
    if (isStreaming) {
      setIsLoadingHistory(false)
      return
    }

    let cancelled = false
    const controller = new AbortController()
    const timeoutId = window.setTimeout(() => controller.abort(), HISTORY_LOAD_TIMEOUT_MS)
    setMessages([])
    setHasMoreHistory(false)
    setIsLoadingOlderHistory(false)
    historyCursorRef.current = null
    setIsLoadingHistory(true)

    fetchMessages(conversationId, INITIAL_HISTORY_LIMIT, undefined, controller.signal)
      .then((res) => {
        if (cancelled) return
        const loaded = res.messages.map(savedMessageToMessage)
        const nextCursor = Number(res.next_before_id)
        historyCursorRef.current = Number.isFinite(nextCursor) ? nextCursor : null
        setMessages(loaded)
        setHasMoreHistory(res.has_more && historyCursorRef.current !== null && loaded.length > 0)
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        if (!cancelled) setMessages([])
      })
      .finally(() => {
        window.clearTimeout(timeoutId)
        if (!cancelled) setIsLoadingHistory(false)
      })

    return () => {
      cancelled = true
      window.clearTimeout(timeoutId)
      controller.abort()
    }
  }, [conversationId, isStreaming, refreshNonce])

  const loadOlderMessages = useCallback(async () => {
    if (!conversationId || isStreaming || isLoadingOlderHistory || !hasMoreHistory) return
    const targetConversationId = conversationId
    const inferredOldestId = Number(messages[0]?.id)
    const beforeId = historyCursorRef.current ?? inferredOldestId
    if (!Number.isFinite(beforeId)) return

    setIsLoadingOlderHistory(true)
    try {
      const res = await fetchMessages(targetConversationId, OLDER_HISTORY_LIMIT, beforeId)
      if (conversationIdRef.current !== targetConversationId) return
      const older = res.messages.map(savedMessageToMessage)
      const nextCursor = Number(res.next_before_id)
      // Drive continuation off the server's has_more plus a strictly-advancing
      // cursor — not off how many rows were new to the current (possibly stale)
      // `messages` snapshot, which would wrongly halt pagination on an
      // all-duplicate page even when older history remains. The advancing-cursor
      // check still guards against an infinite loop.
      const nextCursorAdvances = Number.isFinite(nextCursor) && nextCursor < beforeId
      setMessages((prev) => {
        const currentIds = new Set(prev.map((message) => message.id))
        return [...older.filter((message) => !currentIds.has(message.id)), ...prev]
      })
      historyCursorRef.current = nextCursorAdvances ? nextCursor : null
      setHasMoreHistory(res.has_more && nextCursorAdvances)
    } catch {
      // Keep the visible conversation intact; the user can retry loading older history.
    } finally {
      if (conversationIdRef.current === targetConversationId) {
        setIsLoadingOlderHistory(false)
      }
    }
  }, [conversationId, hasMoreHistory, isLoadingOlderHistory, isStreaming, messages])

  const refreshMessages = useCallback(() => {
    if (!conversationId || isStreaming) return
    setRefreshNonce((value) => value + 1)
  }, [conversationId, isStreaming])

  const sendMessage = useCallback(
    (text: string, attachments: UploadedAttachment[], onConversationCreated: (id: string) => void) => {
      if (!text.trim() || isStreaming) return

      const userMsg: Message = {
        id: crypto.randomUUID(),
        role: 'user',
        content: text,
        attachments,
      }

      const assistantId = crypto.randomUUID()
      const requestStartedAt = performance.now()
      const requestStartedAtMs = Date.now()
      const assistantMsg: Message = {
        id: assistantId,
        role: 'assistant',
        content: '',
        toolCalls: [],
        streaming: true,
        runStatus: 'streaming',
        responseStartedAtMs: requestStartedAtMs,
      }

      setMessages((prev) => [...prev, userMsg, assistantMsg])
      setIsStreaming(true)

      const pendingToolRef = { current: null as ToolCall | null }
      typewriterCleanupRef.current?.()
      const typewriter = {
        queue: '',
        timer: null as ReturnType<typeof setTimeout> | null,
        done: null as StreamDonePayload | null,
        stopped: false,
      }

      const clearTypewriterTimer = () => {
        if (typewriter.timer) {
          clearTimeout(typewriter.timer)
          typewriter.timer = null
        }
      }

      const cleanupTypewriter = () => {
        typewriter.stopped = true
        clearTypewriterTimer()
        // Flush any buffered tokens into the message before discarding the queue
        // so a stream that ends via error/timeout/stop doesn't drop the partial answer.
        const pending = typewriter.queue
        typewriter.queue = ''
        typewriter.done = null
        if (pending) appendAssistantContent(pending)
        if (typewriterCleanupRef.current === cleanupTypewriter) {
          typewriterCleanupRef.current = null
        }
      }

      typewriterCleanupRef.current = cleanupTypewriter

      const appendAssistantContent = (content: string) => {
        if (!content) return
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: m.content + content,
                  contentRevision: (m.contentRevision ?? 0) + 1,
                }
              : m
          )
        )
      }

      const currentResponseDurationMs = () =>
        Math.max(0, Math.round(performance.now() - requestStartedAt))

      const finalizeDone = (done: StreamDonePayload) => {
        if (typewriterCleanupRef.current === cleanupTypewriter) {
          typewriterCleanupRef.current = null
        }
        clearTypewriterTimer()
        setIsStreaming(false)
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: done.incomplete && done.summary && !m.content ? done.summary : m.content,
                  attachments: done.attachments?.length ? done.attachments : m.attachments,
                  streaming: false,
                  runStatus: normalizeRunStatus(done.status) ?? (done.incomplete ? 'paused' : 'complete'),
                  responseDurationMs:
                    done.response_duration_ms ?? currentResponseDurationMs() ?? m.responseDurationMs,
                }
              : m
          )
        )
        const convId = done.conversation_id ?? ''
        if (convId) {
          onConversationCreated(convId)
        }
      }

      const maybeFinalizeDone = () => {
        if (!typewriter.done || typewriter.queue || typewriter.timer) return
        finalizeDone(typewriter.done)
      }

      const flushTypewriter = () => {
        if (typewriter.stopped) return
        const pending = typewriter.queue
        if (!pending) {
          typewriter.timer = null
          maybeFinalizeDone()
          return
        }
        const chunkSize = Math.min(
          TYPEWRITER_MAX_CHARS_PER_TICK,
          pending.length > TYPEWRITER_FAST_BACKLOG_CHARS
            ? TYPEWRITER_CHARS_PER_TICK * 2
            : TYPEWRITER_CHARS_PER_TICK,
        )
        appendAssistantContent(pending.slice(0, chunkSize))
        typewriter.queue = pending.slice(chunkSize)
        if (typewriter.queue) {
          typewriter.timer = setTimeout(flushTypewriter, TYPEWRITER_INTERVAL_MS)
          return
        }
        typewriter.timer = null
        maybeFinalizeDone()
      }

      const enqueueToken = (token: string) => {
        if (!token || typewriter.stopped) return
        typewriter.queue += token
        // Render tokens live as they stream in; the flush drains the queue and
        // (once `done` is set) finalizes the message.
        if (!typewriter.timer) {
          typewriter.timer = setTimeout(flushTypewriter, 0)
        }
      }

      const startTypewriter = () => {
        if (typewriter.stopped || typewriter.timer) return
        // Kick a flush so a queue already fully drained before `done` arrived
        // still triggers finalize via maybeFinalizeDone.
        typewriter.timer = setTimeout(flushTypewriter, 0)
      }

      // Idle timeout: auto-stop if no SSE activity for IDLE_TIMEOUT_MS
      const resetIdleTimer = () => {
        if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
        idleTimerRef.current = setTimeout(() => {
          cleanupTypewriter()
          abortRef.current?.()
          setIsStreaming(false)
          setMessages((prev) =>
            prev.map((m) =>
              m.streaming
                ? { ...m, streaming: false, content: m.content || 'Request timed out — no response received.' }
                : m
            )
          )
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, runStatus: 'error' } : m))
          )
        }, IDLE_TIMEOUT_MS)
      }

      const clearIdleTimer = () => {
        if (idleTimerRef.current) {
          clearTimeout(idleTimerRef.current)
          idleTimerRef.current = null
        }
      }

      resetIdleTimer()

      const addApprovalNotice = (data: ApprovalEvent, type: ApprovalNotice['type']) => {
        const notice: ApprovalNotice = { ...data, type }
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, approvals: [...(m.approvals ?? []), notice] }
              : m
          )
        )
      }

      const updateMsg = (updater: (m: Message) => Message) =>
        setMessages((prev) => prev.map((m) => (m.id === assistantId ? updater(m) : m)))

      const handleThinking = (content: string) => {
        updateMsg((m) => ({ ...m, thinking: (m.thinking ?? '') + content }))
      }

      const handleProgress = (content: string) => {
        const trimmed = content.trim()
        if (!trimmed) return
        resetIdleTimer()
        updateMsg((m) => ({
          ...m,
          activityItems: [
            ...(m.activityItems ?? []),
            { id: crypto.randomUUID(), type: 'progress', content: trimmed },
          ],
        }))
      }

      const handlePlan = (planEvent: PlanEvent) => {
        updateMsg((m) => ({
          ...m,
          plan: planEvent.steps,
          stepProgress: planEvent.steps.map((s) => ({
            step_id: s.step_id,
            description: s.description,
            status: 'pending' as const,
          })),
        }))
      }

      const handleStepStart = (data: StepEvent) => {
        updateMsg((m) => ({
          ...m,
          stepProgress: (m.stepProgress ?? []).map((s) =>
            s.step_id === data.step_id ? { ...s, status: 'active' as const } : s
          ),
        }))
      }

      const handleStepComplete = (data: StepEvent) => {
        updateMsg((m) => ({
          ...m,
          stepProgress: (m.stepProgress ?? []).map((s) =>
            s.step_id === data.step_id ? { ...s, status: (data.status as StepProgress['status']) ?? 'done' } : s
          ),
        }))
      }

      const handleObservation = (data: StepEvent) => {
        updateMsg((m) => ({
          ...m,
          stepProgress: (m.stepProgress ?? []).map((s) =>
            s.step_id === data.step_id ? { ...s, observation: data.detail } : s
          ),
        }))
      }

      const handleRetry = (data: StepEvent) => {
        updateMsg((m) => ({
          ...m,
          stepProgress: (m.stepProgress ?? []).map((s) =>
            s.step_id === data.step_id
              ? { ...s, status: 'retrying' as const, retry_count: data.attempt }
              : s
          ),
        }))
      }

      const handleAccessGrantRequired = (data: AccessGrantRequiredEvent) => {
        resetIdleTimer()
        setPendingAccessGrant({
          ticket_id: data.ticket_id,
          target_type: data.target_type,
          target_identifier: data.target_identifier,
          display_name: data.display_name,
          action_context: data.action_context,
          requested_access: data.requested_access,
        })
      }

      const abort = chatStream(
        text,
        conversationId,
        attachments,
        // onToken
        (token) => {
          resetIdleTimer()
          enqueueToken(token)
        },
        // onToolStart
        (data) => {
          resetIdleTimer()
          const toolId = data.call_id ?? crypto.randomUUID()
          const tc: ToolCall = {
            id: toolId,
            tool: data.tool,
            input: typeof data.input === 'string' ? data.input : JSON.stringify(data.input, null, 2),
            pending: true,
          }
          pendingToolRef.current = tc
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? {
                    ...m,
                    toolCalls: [...(m.toolCalls ?? []), tc],
                    activityItems: [...(m.activityItems ?? []), { id: toolId, type: 'tool', toolCall: tc }],
                  }
                : m
            )
          )
        },
        // onToolEnd
        (data) => {
          resetIdleTimer()
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== assistantId) return m
              const tcs = [...(m.toolCalls ?? [])]
              const toolIndex = data.call_id
                ? tcs.findIndex((tc) => tc.id === data.call_id)
                : tcs.map((tc, index) => ({ tc, index })).reverse().find((item) => item.tc.pending)?.index ?? -1
              if (toolIndex >= 0) {
                tcs[toolIndex] = {
                  ...tcs[toolIndex],
                  output: data.output,
                  pending: false,
                }
              }
              return {
                ...m,
                toolCalls: tcs,
                activityItems: (m.activityItems ?? []).map((item) =>
                  item.type === 'tool' && toolIndex >= 0 && item.toolCall.id === tcs[toolIndex].id
                    ? { ...item, toolCall: tcs[toolIndex] }
                    : item
                ),
              }
            })
          )
        },
        // onDone
        (done) => {
          clearIdleTimer()
          typewriter.done = done
          startTypewriter()
        },
        // onError
        (err, data) => {
          cleanupTypewriter()
          clearIdleTimer()
          setIsStreaming(false)
          const isPause = data?.code?.startsWith('iteration_limit_')
            || data?.code === 'iteration_budget_exhausted'
            || data?.code === 'model_output_truncated'
            || data?.code === 'stalled_repeat_detected'
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? {
                    ...m,
                    content: m.content || (isPause ? err : `Error: ${err}`),
                    streaming: false,
                    runStatus: isPause ? 'paused' : 'error',
                    responseDurationMs: currentResponseDurationMs() ?? m.responseDurationMs,
                  }
                : m
            )
          )
        },
        // onApprovalRequired
        (data) => addApprovalNotice(data, 'required'),
        // onApprovalResolved
        (data) => addApprovalNotice(data, 'resolved'),
        // onExecutionResumed
        (data) => addApprovalNotice(data, 'resumed'),
        handleThinking,
        handleProgress,
        handlePlan,
        handleStepStart,
        handleStepComplete,
        handleObservation,
        handleRetry,
        handleAccessGrantRequired,
        // onToolResumed — update the last pending tool card with the resolved result
        (data) => {
          resetIdleTimer()
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== assistantId) return m
              const tcs = [...(m.toolCalls ?? [])]
              // Find the last tool card that matches and is still pending
              const idx = [...tcs].reverse().findIndex((tc) => tc.tool === data.tool && tc.pending)
              if (idx === -1) return m
              const realIdx = tcs.length - 1 - idx
              tcs[realIdx] = { ...tcs[realIdx], output: data.output, pending: false }
              return {
                ...m,
                toolCalls: tcs,
                activityItems: (m.activityItems ?? []).map((item) =>
                  item.type === 'tool' && item.toolCall.id === tcs[realIdx].id
                    ? { ...item, toolCall: tcs[realIdx] }
                    : item
                ),
              }
            })
          )
        },
        // onPing — keepalive while waiting for permission dialog, reset idle timer
        () => { resetIdleTimer() },
        // onHeartbeat — backend idle heartbeat, reset idle timer
        () => { resetIdleTimer() },
        () => { resetIdleTimer() },
      )

      abortRef.current = abort
    },
    [conversationId, isStreaming]
  )

  const stopStreaming = useCallback(() => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    typewriterCleanupRef.current?.()
    abortRef.current?.()
    setIsStreaming(false)
    setMessages((prev) =>
      prev.map((m) => (m.streaming ? { ...m, streaming: false, runStatus: 'paused' } : m))
    )
  }, [])

  const clearMessages = useCallback(() => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    typewriterCleanupRef.current?.()
    setMessages([])
    setIsStreaming(false)
    setHasMoreHistory(false)
    historyCursorRef.current = null
  }, [])

  return {
    messages,
    isStreaming,
    isLoadingHistory,
    isLoadingOlderHistory,
    hasMoreHistory,
    pendingAccessGrant,
    setPendingAccessGrant,
    loadOlderMessages,
    sendMessage,
    refreshMessages,
    stopStreaming,
    clearMessages,
  }
}
