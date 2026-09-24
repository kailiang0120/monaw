import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { Loader2 } from 'lucide-react'
import { MessageBubble } from './MessageBubble'
import type { Message } from '../hooks/useChat'
import { DEFAULT_AGENT_NAME, resolveAgentName } from '../lib/identity'
import startMascotImg from '../assets/mascots/idle.webp'
import stillMascotImg from '../assets/Logo.png'

const VIRTUALIZE_MESSAGE_THRESHOLD = 40
const VIRTUAL_OVERSCAN = 6
const VIRTUAL_VIEWPORT_FALLBACK_PX = 720
const MIN_ESTIMATED_MESSAGE_HEIGHT_PX = 88
const MAX_ESTIMATED_MESSAGE_HEIGHT_PX = 760

function estimateMessageHeight(message: Message): number {
  const contentLength = message.content.length
  const attachmentCount = message.attachments?.length ?? 0
  if (message.role === 'user') {
    return Math.min(
      MAX_ESTIMATED_MESSAGE_HEIGHT_PX,
      Math.max(MIN_ESTIMATED_MESSAGE_HEIGHT_PX, 76 + Math.ceil(contentLength / 90) * 20 + attachmentCount * 44),
    )
  }

  const toolCount = message.toolCalls?.length ?? 0
  const stepCount = message.stepProgress?.length ?? 0
  return Math.min(
    MAX_ESTIMATED_MESSAGE_HEIGHT_PX,
    Math.max(
      MIN_ESTIMATED_MESSAGE_HEIGHT_PX,
      132 + Math.ceil(contentLength / 90) * 22 + Math.min(toolCount, 4) * 46 + Math.min(stepCount, 4) * 28 + attachmentCount * 72,
    ),
  )
}

interface Props {
  messages: Message[]
  isLoadingHistory?: boolean
  isLoadingOlderHistory?: boolean
  hasMoreHistory?: boolean
  conversationId?: string | null
  isStreaming?: boolean
  agentName?: string
  onLoadOlderMessages?: () => void
  onPromptSelect?: (prompt: string) => void // kept for API compat
}

export function ChatWindow({
  messages,
  isLoadingHistory,
  isLoadingOlderHistory = false,
  hasMoreHistory = false,
  conversationId,
  agentName = DEFAULT_AGENT_NAME,
  onLoadOlderMessages,
}: Props) {
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const scrollFrameRef = useRef<number | null>(null)
  const followLatestRef = useRef(true)
  const [transcriptViewport, setTranscriptViewport] = useState({
    scrollTop: 0,
    height: VIRTUAL_VIEWPORT_FALLBACK_PX,
  })
  const assistantLabel = resolveAgentName(agentName)
  const latestMessage = messages[messages.length - 1]
  const latestMessageId = latestMessage?.id
  const latestMessageStartsTurn = latestMessage?.role === 'assistant'
    && latestMessage.streaming === true
    && messages[messages.length - 2]?.role === 'user'
  const followLatest = useCallback(() => {
    const container = scrollContainerRef.current
    if (!container || !followLatestRef.current) return
    container.scrollTop = container.scrollHeight
  }, [])
  const scheduleFollowLatest = useCallback(() => {
    if (scrollFrameRef.current !== null) return
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null
      followLatest()
    })
  }, [followLatest])
  const updateTranscriptViewport = useCallback((container: HTMLDivElement | null) => {
    if (!container) return
    const next = {
      scrollTop: container.scrollTop,
      height: container.clientHeight || VIRTUAL_VIEWPORT_FALLBACK_PX,
    }
    setTranscriptViewport((current) => (
      Math.abs(current.scrollTop - next.scrollTop) < 1 && Math.abs(current.height - next.height) < 1
        ? current
        : next
    ))
  }, [])
  const handleTranscriptScroll = useCallback((container: HTMLDivElement) => {
    const distanceToBottom = container.scrollHeight - container.clientHeight - container.scrollTop
    followLatestRef.current = distanceToBottom <= 80
    updateTranscriptViewport(container)
  }, [updateTranscriptViewport])

  useLayoutEffect(() => {
    const container = scrollContainerRef.current
    updateTranscriptViewport(container)
    if (!container || typeof ResizeObserver === 'undefined') return

    const resizeObserver = new ResizeObserver(() => updateTranscriptViewport(container))
    resizeObserver.observe(container)
    return () => resizeObserver.disconnect()
  }, [updateTranscriptViewport])

  useEffect(() => {
    if (latestMessageStartsTurn) followLatestRef.current = true
    scheduleFollowLatest()
  }, [latestMessageId, latestMessageStartsTurn, scheduleFollowLatest])

  useEffect(() => {
    followLatestRef.current = true
    scheduleFollowLatest()
  }, [conversationId, scheduleFollowLatest])

  useEffect(() => {
    const content = contentRef.current
    if (!content || typeof ResizeObserver === 'undefined') return
    const resizeObserver = new ResizeObserver(scheduleFollowLatest)
    resizeObserver.observe(content)
    return () => resizeObserver.disconnect()
  }, [scheduleFollowLatest])

  useEffect(() => {
    return () => {
      if (scrollFrameRef.current !== null) {
        window.cancelAnimationFrame(scrollFrameRef.current)
        scrollFrameRef.current = null
      }
    }
  }, [])


  return (
    <section className="relative flex min-h-0 flex-1 flex-col bg-[#11100f]">
      {/* The title lives in the app bar; this only fades the transcript under it. */}
      <div className="chat-title-fade pointer-events-none absolute inset-x-0 top-0 z-20 h-8" aria-hidden="true" />

      {isLoadingHistory && messages.length === 0 ? (
        <div className="flex flex-1 select-none flex-col items-center justify-center gap-3 text-neutral-500">
          <Loader2 size={24} className="animate-spin text-neutral-500" />
          <p className="text-sm font-medium">Loading conversation</p>
        </div>
      ) : messages.length === 0 ? (
        <div className="flex flex-1 select-none flex-col items-center justify-center gap-5 px-4">
          <picture>
            <source media="(prefers-reduced-motion: reduce)" srcSet={stillMascotImg} />
            <img
              src={startMascotImg}
              alt={assistantLabel}
              className="h-28 w-28 object-contain drop-shadow-[0_8px_24px_rgba(79,122,43,0.18)]"
              draggable={false}
            />
          </picture>
          <div className="text-center">
            <p className="text-base font-medium tracking-tight text-neutral-400">{assistantLabel}</p>
            <p className="mt-0.5 text-[13px] text-neutral-600">Ready.</p>
          </div>
        </div>
      ) : (
        <div
          ref={scrollContainerRef}
          aria-label="Chat transcript"
          onScroll={(event) => handleTranscriptScroll(event.currentTarget)}
          className="flex-1 overflow-y-auto px-4 pb-6 pt-8"
        >
          <div ref={contentRef} className="mx-auto max-w-3xl">
            {hasMoreHistory && (
              <div className="mb-5 flex justify-center">
                <button
                  type="button"
                  onClick={onLoadOlderMessages}
                  disabled={isLoadingOlderHistory}
                  className="inline-flex items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.035] px-3 py-1.5 text-xs font-medium text-neutral-400 transition-colors hover:bg-white/[0.06] hover:text-neutral-200 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {isLoadingOlderHistory && <Loader2 size={13} className="animate-spin" />}
                  {isLoadingOlderHistory ? 'Loading earlier' : 'Load earlier messages'}
                </button>
              </div>
            )}
            <MessageList
              agentName={assistantLabel}
              messages={messages}
              scrollContainerRef={scrollContainerRef}
              viewport={transcriptViewport}
            />
          </div>
        </div>
      )}

    </section>
  )
}

function MessageList({
  messages,
  agentName,
  scrollContainerRef,
  viewport,
}: {
  messages: Message[]
  agentName: string
  scrollContainerRef: RefObject<HTMLDivElement>
  viewport: { scrollTop: number; height: number }
}) {
  if (messages.length < VIRTUALIZE_MESSAGE_THRESHOLD) {
    return (
      <>
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} agentName={agentName} />
        ))}
      </>
    )
  }

  return (
    <VirtualizedMessageList
      agentName={agentName}
      messages={messages}
      scrollContainerRef={scrollContainerRef}
      viewport={viewport}
    />
  )
}

function VirtualizedMessageList({
  messages,
  agentName,
  scrollContainerRef,
  viewport,
}: {
  messages: Message[]
  agentName: string
  scrollContainerRef: RefObject<HTMLDivElement>
  viewport: { scrollTop: number; height: number }
}) {
  const heightByIdRef = useRef<Map<string, number>>(new Map())
  const previousLayoutRef = useRef<{ firstId?: string; lastId?: string; totalHeight: number } | null>(null)
  const [heightRevision, setHeightRevision] = useState(0)

  useEffect(() => {
    const visibleIds = new Set(messages.map((message) => message.id))
    let removed = false
    for (const id of heightByIdRef.current.keys()) {
      if (!visibleIds.has(id)) {
        heightByIdRef.current.delete(id)
        removed = true
      }
    }
    if (removed) setHeightRevision((value) => value + 1)
  }, [messages])

  const onHeightChange = useCallback((id: string, height: number) => {
    if (!Number.isFinite(height) || height <= 0) return
    const previous = heightByIdRef.current.get(id)
    if (previous !== undefined && Math.abs(previous - height) < 1) return
    heightByIdRef.current.set(id, height)
    setHeightRevision((value) => value + 1)
  }, [])

  const layout = useMemo(() => {
    const offsets: number[] = [0]
    for (const message of messages) {
      const measuredHeight = heightByIdRef.current.get(message.id)
      offsets.push(offsets[offsets.length - 1] + (measuredHeight ?? estimateMessageHeight(message)))
    }

    const totalHeight = offsets[offsets.length - 1] ?? 0
    const viewportTop = Math.max(0, Math.min(viewport.scrollTop, Math.max(0, totalHeight - viewport.height)))
    const viewportBottom = viewportTop + Math.max(viewport.height, VIRTUAL_VIEWPORT_FALLBACK_PX)
    let start = 0
    while (start < messages.length && offsets[start + 1] < viewportTop) {
      start += 1
    }
    start = Math.max(0, start - VIRTUAL_OVERSCAN)

    let end = start
    while (end < messages.length && offsets[end] <= viewportBottom) {
      end += 1
    }
    end = Math.min(messages.length, end + VIRTUAL_OVERSCAN)

    const topPadding = offsets[start] ?? 0
    const bottomPadding = Math.max(0, totalHeight - (offsets[end] ?? totalHeight))
    return {
      bottomPadding,
      end,
      start,
      topPadding,
      totalHeight,
      visibleMessages: messages.slice(start, end),
    }
  }, [heightRevision, messages, viewport])

  useLayoutEffect(() => {
    const firstId = messages[0]?.id
    const lastId = messages[messages.length - 1]?.id
    const previous = previousLayoutRef.current
    if (
      previous
      && firstId !== previous.firstId
      && lastId === previous.lastId
      && scrollContainerRef.current
    ) {
      const heightDelta = layout.totalHeight - previous.totalHeight
      if (heightDelta > 0) {
        scrollContainerRef.current.scrollTop += heightDelta
      }
    }
    previousLayoutRef.current = { firstId, lastId, totalHeight: layout.totalHeight }
  }, [layout.totalHeight, messages, scrollContainerRef])

  return (
    <div aria-label="Conversation messages">
      {layout.topPadding > 0 && <div aria-hidden style={{ height: layout.topPadding }} />}
      {layout.visibleMessages.map((message) => (
        <MeasuredMessageRow
          key={message.id}
          agentName={agentName}
          message={message}
          onHeightChange={onHeightChange}
        />
      ))}
      {layout.bottomPadding > 0 && <div aria-hidden style={{ height: layout.bottomPadding }} />}
    </div>
  )
}

function MeasuredMessageRow({
  message,
  agentName,
  onHeightChange,
}: {
  message: Message
  agentName: string
  onHeightChange: (id: string, height: number) => void
}) {
  const rowRef = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    const node = rowRef.current
    if (!node) return

    let frame = 0
    const measure = () => {
      frame = 0
      onHeightChange(message.id, node.getBoundingClientRect().height)
    }
    const scheduleMeasure = () => {
      if (frame) return
      frame = window.requestAnimationFrame(measure)
    }

    scheduleMeasure()
    if (typeof ResizeObserver === 'undefined') {
      return () => {
        if (frame) window.cancelAnimationFrame(frame)
      }
    }

    const observer = new ResizeObserver(scheduleMeasure)
    observer.observe(node)
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      observer.disconnect()
    }
  }, [message.id, onHeightChange])

  return (
    <div ref={rowRef} className="flow-root">
      <MessageBubble message={message} agentName={agentName} />
    </div>
  )
}
