import { useState, useCallback, useRef, useEffect } from 'react'
import { chatStream } from '../lib/api/chatStream'
import { ApiError } from '../lib/api/client'
import { fetchChatRuns, newRunId, sendSteeringMessage, stopChatRun } from '../lib/api/steering'
import { fetchMessages } from '../lib/api/conversations'
import {
  type ApprovalEvent,
  type PlanEvent,
  type PlanStep,
  type StepEvent,
  type AccessGrantRequiredEvent,
  type SavedMessage,
} from '../lib/api/types'
import type { ChatRun, UploadedAttachment } from '../lib/api/types'

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
  // From history, where the output may be a preview too short to parse.
  onHost?: boolean
}

export type ActivityItem =
  | { id: string; type: 'progress'; content: string }
  | { id: string; type: 'tool'; toolCall: ToolCall }

export interface ApprovalNotice extends ApprovalEvent {
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
  runId?: string
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
  steeringStatus?: 'queued' | 'applied'
}

/** A message waiting to be sent as the next turn once the current one ends. */
export interface QueuedMessage {
  id: string
  text: string
  attachments: UploadedAttachment[]
  conversationId: string | null
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
          onHost: tc.on_host ?? false,
        }))
      : undefined,
  }
}

export function useChat(conversationId: string | null) {
  const [messages, setMessages] = useState<Message[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [steeringConversationId, setSteeringConversationId] = useState<string | null>(null)
  const steeringTargetRef = useRef<{ runId: string; conversationId: string; assistantId: string } | null>(null)
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [isLoadingOlderHistory, setIsLoadingOlderHistory] = useState(false)
  const [hasMoreHistory, setHasMoreHistory] = useState(false)
  const [pendingAccessGrant, setPendingAccessGrant] = useState<AccessGrantNotice | null>(null)
  const [refreshNonce, setRefreshNonce] = useState(0)
  const [queued, setQueued] = useState<QueuedMessage[]>([])
  // Stop holds the queue until the user sends again.
  const [queuePaused, setQueuePaused] = useState(false)
  // Steering messages delivered to the running turn but not yet read by it.
  const pendingSteeringRef = useRef<Map<string, QueuedMessage>>(new Map())
  // The applied event can beat the steer request's own reply.
  const appliedSteeringIdsRef = useRef<Set<string>>(new Set())
  const abortRef = useRef<(() => void) | null>(null)
  // The turn being watched. Closing its stream only stops watching; this ID ends it.
  const runIdRef = useRef<string | null>(null)
  const endRun = useCallback(() => {
    const runId = runIdRef.current
    runIdRef.current = null
    if (runId) void stopChatRun(runId).catch(() => {})
  }, [])
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const typewriterCleanupRef = useRef<(() => void) | null>(null)
  const conversationIdRef = useRef<string | null>(conversationId)
  const streamConversationIdRef = useRef<string | null>(conversationId)
  const historyCursorRef = useRef<number | null>(null)
  const skipCompletedTurnReloadRef = useRef<string | null>(null)
  const preserveUnboundTurnRef = useRef(false)
  const displayedConversationIdRef = useRef<string | null>(null)
  const followRunRef = useRef<((run: ChatRun) => void) | null>(null)

  useEffect(() => {
    conversationIdRef.current = conversationId
  }, [conversationId])

  // Load saved messages when conversationId changes
  useEffect(() => {
    if (!conversationId) {
      if (!isStreaming && !preserveUnboundTurnRef.current) setMessages([])
      setHasMoreHistory(false)
      setIsLoadingOlderHistory(false)
      historyCursorRef.current = null
      return
    }
    if (isStreaming) {
      setIsLoadingHistory(false)
      return
    }
    if (skipCompletedTurnReloadRef.current === conversationId) {
      skipCompletedTurnReloadRef.current = null
      preserveUnboundTurnRef.current = false
      displayedConversationIdRef.current = conversationId
      setIsLoadingHistory(false)
      return
    }

    let cancelled = false
    const controller = new AbortController()
    const timeoutId = window.setTimeout(() => controller.abort(), HISTORY_LOAD_TIMEOUT_MS)
    if (displayedConversationIdRef.current !== conversationId) setMessages([])
    setHasMoreHistory(false)
    setIsLoadingOlderHistory(false)
    historyCursorRef.current = null
    setIsLoadingHistory(true)

    fetchMessages(conversationId, INITIAL_HISTORY_LIMIT, undefined, controller.signal)
      .then((res) => {
        if (cancelled) return
        const loaded = res.messages.map(savedMessageToMessage)
        displayedConversationIdRef.current = conversationId
        const nextCursor = Number(res.next_before_id)
        historyCursorRef.current = Number.isFinite(nextCursor) ? nextCursor : null
        setMessages(loaded)
        setHasMoreHistory(res.has_more && historyCursorRef.current !== null && loaded.length > 0)
        // A turn may still be running here: left for another chat, or the window reloaded.
        Promise.resolve()
          .then(() => fetchChatRuns(controller.signal))
          .then((runs) => {
            const run = runs?.find((item) => item.conversation_id === conversationId)
            if (run && !cancelled) followRunRef.current?.(run)
          })
          .catch(() => {})
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

  const queueMessage = useCallback((text: string, attachments: UploadedAttachment[] = []) => {
    const conversation = steeringTargetRef.current?.conversationId ?? conversationIdRef.current
    setQueued((current) => [...current, { id: crypto.randomUUID(), text, attachments, conversationId: conversation }])
  }, [])

  const removeQueued = useCallback((id: string) => {
    setQueued((current) => current.filter((item) => item.id !== id))
  }, [])

  const resumeQueue = useCallback(() => setQueuePaused(false), [])

  // Steering messages the agent never read run as the next turn instead.
  const requeueLateSteering = useCallback(() => {
    const late = [...pendingSteeringRef.current.values()]
    pendingSteeringRef.current.clear()
    if (!late.length) return
    const lateIds = new Set(late.map((item) => item.id))
    setMessages((prev) => prev.filter((m) => !lateIds.has(m.id)))
    setQueued((current) => [...late, ...current.filter((item) => !lateIds.has(item.id))])
  }, [])

  const steerMessage = useCallback(async (text: string, attachments: UploadedAttachment[] = []) => {
    const target = steeringTargetRef.current
    if (!target || !abortRef.current) {
      queueMessage(text, attachments)
      return
    }
    const messageId = crypto.randomUUID()
    let response
    try {
      response = await sendSteeringMessage(target.runId, messageId, text, attachments)
    } catch (error) {
      // The turn closed before the message arrived: it runs as the next turn.
      if (error instanceof ApiError && error.status === 409) {
        queueMessage(text, attachments)
        return
      }
      throw error
    }
    if (!abortRef.current) {
      if (response.status !== 'applied') queueMessage(text, attachments)
      return
    }
    if (steeringTargetRef.current !== target) throw new Error('The task changed before delivery was confirmed. Your draft has been kept.')
    const applied = response.status === 'applied' || appliedSteeringIdsRef.current.has(messageId)
    if (!applied) pendingSteeringRef.current.set(messageId, { id: messageId, text, attachments, conversationId: target.conversationId })
    // It goes below the reply so far; once the agent reads it, the reply continues under it.
    setMessages((current) => {
      if (current.some((item) => item.id === messageId)) return current
      if (!current.some((item) => item.id === target.assistantId)) return current
      const steeringMessage: Message = {
        id: messageId, role: 'user', content: text, attachments,
        steeringStatus: applied ? 'applied' : 'queued',
      }
      return [...current, steeringMessage]
    })
  }, [queueMessage])

  const sendMessage = useCallback(
    (
      text: string,
      attachments: UploadedAttachment[],
      onConversationCreated: (id: string) => void,
      workspaceDir?: string,
      // Watch this turn that is already running (a chat reopened mid-turn) instead of starting one.
      follow?: ChatRun,
    ) => {
      if (!text.trim() || isStreaming) return
      steeringTargetRef.current = null
      setSteeringConversationId(null)
      pendingSteeringRef.current.clear()
      appliedSteeringIdsRef.current.clear()
      setQueuePaused(false)

      const runId = follow?.run_id ?? newRunId()
      const userMsg: Message = {
        id: crypto.randomUUID(),
        role: 'user',
        content: text,
        attachments,
      }

      // Each applied steering message starts a new assistant message (see onSteeringApplied).
      let assistantId = crypto.randomUUID()
      streamConversationIdRef.current = conversationId
      preserveUnboundTurnRef.current = conversationId === null
      const runAgeMs = follow ? Math.max(0, Date.now() - follow.started_at * 1000) : 0
      const requestStartedAt = performance.now() - runAgeMs
      const requestStartedAtMs = Date.now() - runAgeMs
      const assistantMsg: Message = {
        id: assistantId,
        runId,
        role: 'assistant',
        content: '',
        toolCalls: [],
        streaming: true,
        runStatus: 'streaming',
        responseStartedAtMs: requestStartedAtMs,
      }

      // A resumed Run continues below the paused reply; its prompt is not the user's.
      const continuation = Boolean(follow?.continuation)
      setMessages((prev) => continuation ? [...prev, assistantMsg] : [...prev, userMsg, assistantMsg])
      setIsStreaming(true)

      const pendingToolRef = { current: null as ToolCall | null }
      typewriterCleanupRef.current?.()
      const typewriter = {
        queue: '',
        timer: null as ReturnType<typeof setTimeout> | null,
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
        abortRef.current = null
        if (typewriterCleanupRef.current === cleanupTypewriter) {
          typewriterCleanupRef.current = null
        }
        clearTypewriterTimer()
        skipCompletedTurnReloadRef.current = done.conversation_id ?? conversationId
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
        requeueLateSteering()
        const convId = done.conversation_id ?? ''
        if (convId) {
          onConversationCreated(convId)
        }
      }

      const flushTypewriter = () => {
        if (typewriter.stopped) return
        const pending = typewriter.queue
        if (!pending) {
          typewriter.timer = null
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

      // Idle timeout: auto-stop if no SSE activity for IDLE_TIMEOUT_MS
      const resetIdleTimer = () => {
        if (typewriter.stopped) return
        if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
        idleTimerRef.current = setTimeout(() => {
          cleanupTypewriter()
          endRun()
          abortRef.current?.()
          abortRef.current = null
          skipCompletedTurnReloadRef.current = conversationId
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
          requeueLateSteering()
          setQueuePaused(true)
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
        if (typewriter.stopped || !data.ticket_id) return
        const notice: ApprovalNotice = { ...data, type }
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== assistantId) return m
            const approvals = m.approvals ?? []
            return { ...m, approvals: approvals.some((entry) => entry.ticket_id === notice.ticket_id)
              ? approvals.map((entry) => entry.ticket_id === notice.ticket_id ? { ...entry, ...notice } : entry)
              : [...approvals, notice] }
          })
        )
      }

      const updateMsg = (updater: (m: Message) => Message) => {
        if (typewriter.stopped) return
        setMessages((prev) => prev.map((m) => (m.id === assistantId ? updater(m) : m)))
      }

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
        if (typewriter.stopped) return
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

      runIdRef.current = runId
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
          if (typewriter.stopped) return
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
          if (typewriter.stopped) return
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
                  status: data.status,
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
          if (typewriter.stopped) return
          clearTypewriterTimer()
          const pending = typewriter.queue
          typewriter.queue = ''
          if (pending) appendAssistantContent(pending)
          typewriter.stopped = true
          finalizeDone(done)
        },
        // onError
        (err, data) => {
          if (typewriter.stopped) return
          cleanupTypewriter()
          clearIdleTimer()
          abortRef.current = null
          if (follow && data?.code === 'not_found') {
            // The turn ended before this chat reconnected; its saved history has the result.
            runIdRef.current = null
            setIsStreaming(false)
            setMessages((prev) => prev.filter((m) => m.id !== userMsg.id && m.id !== assistantId))
            setRefreshNonce((value) => value + 1)
            return
          }
          skipCompletedTurnReloadRef.current = conversationId
          setIsStreaming(false)
          const isPause = data?.code?.startsWith('iteration_limit_')
            || data?.code === 'iteration_budget_exhausted'
            || data?.code === 'token_budget_exhausted'
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
          requeueLateSteering()
          // A failed turn holds the queue rather than sending into the same failure.
          if (!isPause) setQueuePaused(true)
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
          if (typewriter.stopped) return
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
        // onHeartbeat and transport activity keep the turn alive.
        () => { resetIdleTimer() },
        () => { resetIdleTimer() },
        // The server assigns a steering target even for a brand new chat.
        (data) => {
          if (typewriter.stopped) return
          steeringTargetRef.current = { runId: data.run_id, conversationId: data.conversation_id, assistantId }
          setSteeringConversationId(data.conversation_id)
        },
        (data) => {
          if (typewriter.stopped) return
          resetIdleTimer()
          pendingSteeringRef.current.delete(data.message_id)
          appliedSteeringIdsRef.current.add(data.message_id)
          if (data.reset_response) {
            clearTypewriterTimer()
            typewriter.queue = ''
          }
          // Show the instruction where the agent read it: the work so far stays
          // above it and the rest of the reply continues in a new message below.
          // It is still one task, so the timer moves down and keeps counting.
          const previousId = assistantId
          const nextId = crypto.randomUUID()
          assistantId = nextId
          // Updated in place: a steer awaiting its reply checks the target by identity.
          if (steeringTargetRef.current?.assistantId === previousId) steeringTargetRef.current.assistantId = nextId
          setMessages((current) => {
            const existing = current.find((item) => item.id === data.message_id)
            const steer: Message = {
              ...(existing ?? { id: data.message_id, role: 'user' as const, content: data.message, attachments: data.attachments }),
              steeringStatus: 'applied',
            }
            const rest = current.filter((item) => item.id !== data.message_id)
            const index = rest.findIndex((item) => item.id === previousId)
            if (index < 0) return current
            const previous = rest[index]
            const kept = data.reset_response ? { ...previous, content: '' } : previous
            // A segment with nothing in it yet (two steers read together) is replaced, not kept.
            const empty = !kept.content && !kept.toolCalls?.length && !kept.activityItems?.length && !kept.approvals?.length
            const next: Message = {
              id: nextId, role: 'assistant', content: '', toolCalls: [], streaming: true, runStatus: 'streaming',
              runId,
              responseStartedAtMs: previous.responseStartedAtMs,
            }
            const done: Message[] = empty ? [] : [{
              ...kept, streaming: false, runStatus: 'complete', responseStartedAtMs: undefined, responseDurationMs: undefined,
              contentRevision: (kept.contentRevision ?? 0) + 1,
            }]
            return [...rest.slice(0, index), ...done, steer, next, ...rest.slice(index + 1)]
          })
        },
        workspaceDir,
        runId,
        follow?.run_id,
      )

      abortRef.current = abort
    },
    [conversationId, isStreaming, requeueLateSteering]
  )

  followRunRef.current = (run: ChatRun) =>
    sendMessage(run.message.trim() || 'Please review the attached file(s).', run.attachments, () => {}, undefined, run)

  /** Watch a turn that started outside this view, such as a resumed Run. False if it is not running. */
  const followRun = useCallback(async (runId: string): Promise<boolean> => {
    const runs = await fetchChatRuns()
    const run = runs?.find((item) => item.run_id === runId)
    if (!run || run.conversation_id !== conversationIdRef.current) return false
    followRunRef.current?.(run)
    return true
  }, [])

  const stopStreaming = useCallback(() => {
    endRun()
    steeringTargetRef.current = null
    setSteeringConversationId(null)
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    typewriterCleanupRef.current?.()
    abortRef.current?.()
    abortRef.current = null
    skipCompletedTurnReloadRef.current = streamConversationIdRef.current === conversationIdRef.current
      ? conversationIdRef.current : null
    setIsStreaming(false)
    setMessages((prev) =>
      prev.map((m) => (m.streaming ? { ...m, streaming: false, runStatus: 'paused' } : m))
    )
    requeueLateSteering()
    setQueuePaused(true)
  }, [requeueLateSteering])

  const clearMessages = useCallback(() => {
    // New chat: the running turn keeps going in the background.
    runIdRef.current = null
    steeringTargetRef.current = null
    setSteeringConversationId(null)
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    typewriterCleanupRef.current?.()
    abortRef.current?.()
    abortRef.current = null
    skipCompletedTurnReloadRef.current = null
    pendingSteeringRef.current.clear()
    preserveUnboundTurnRef.current = false
    displayedConversationIdRef.current = null
    setMessages([])
    setPendingAccessGrant(null)
    setIsStreaming(false)
    setHasMoreHistory(false)
    historyCursorRef.current = null
  }, [])

  useEffect(() => {
    if (abortRef.current && streamConversationIdRef.current !== conversationId) {
      // Opening another chat stops watching this turn, not the turn itself:
      // it finishes in the background and its chat reloads when it does.
      runIdRef.current = null
      steeringTargetRef.current = null
      setSteeringConversationId(null)
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
      typewriterCleanupRef.current?.()
      abortRef.current?.()
      abortRef.current = null
      skipCompletedTurnReloadRef.current = null
      // The turn may still read these in the background; its saved history shows which.
      pendingSteeringRef.current.clear()
      setIsStreaming(false)
      setPendingAccessGrant(null)
    }
  }, [conversationId])

  useEffect(() => () => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    typewriterCleanupRef.current?.()
    abortRef.current?.()
    abortRef.current = null
  }, [])

  return {
    steerMessage,
    steeringConversationId,
    queued,
    queuePaused,
    queueMessage,
    removeQueued,
    resumeQueue,
    messages,
    isStreaming,
    isLoadingHistory,
    isLoadingOlderHistory,
    hasMoreHistory,
    pendingAccessGrant,
    setPendingAccessGrant,
    loadOlderMessages,
    sendMessage,
    followRun,
    refreshMessages,
    stopStreaming,
    clearMessages,
  }
}
