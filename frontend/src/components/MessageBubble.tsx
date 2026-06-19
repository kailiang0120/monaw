import { memo, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  AlertTriangle,
  BrainCircuit,
  Check,
  CheckCircle2,
  CircleDashed,
  ChevronDown,
  ChevronRight,
  Download,
  FileText,
  Image,
  Loader2,
  Play,
  Timer,
  Wrench,
  X,
  XCircle,
} from 'lucide-react'
import mascotDefault from '../assets/mascots/mascot.png'
import mascotGood from '../assets/mascots/good.png'
import mascotCurious from '../assets/mascots/curious.png'
import mascotStart from '../assets/mascots/start.png'
import mascotThinking from '../assets/mascots/thinking.gif'
import { PlanProgress } from './PlanProgress'
import { approveTicket, rejectTicket } from '../lib/api/approvals'
import { fetchMessageToolCalls } from '../lib/api/conversations'
import { downloadAttachment, fetchAttachmentObjectUrl } from '../lib/api/files'
import { formatAgentResponse } from '../lib/formatAgentResponse'
import { DEFAULT_AGENT_NAME, resolveAgentName } from '../lib/identity'
import type { UploadedAttachment } from '../lib/api/types'
import type { ActivityItem, ApprovalNotice, Message, ToolCall } from '../hooks/useChat'

const MIN_IMAGE_PREVIEW_DIMENSION_PX = 16
const MAX_VISIBLE_THINKING_CHARS = 500

function isDisplayableThinking(thinking?: string): thinking is string {
  const trimmed = thinking?.trim()
  return Boolean(trimmed && trimmed.length <= MAX_VISIBLE_THINKING_CHARS)
}

function ThinkingStatusIcon({ className = '' }: { className?: string }) {
  return (
    <span className={`relative flex h-4 w-4 shrink-0 items-center justify-center text-accent-light ${className}`} aria-hidden>
      <span className="absolute h-3.5 w-3.5 animate-ping rounded-full border border-accent-light/35" />
      <BrainCircuit size={15} strokeWidth={2.2} className="relative" />
    </span>
  )
}

function TypingDots({ className = '' }: { className?: string }) {
  return (
    <span
      className={`inline-flex shrink-0 items-end gap-[3px] ${className}`}
      aria-hidden
    >
      <span
        className="h-1.5 w-1.5 rounded-full bg-accent-light animate-typing-dot"
        style={{ animationDelay: '0ms' }}
      />
      <span
        className="h-1.5 w-1.5 rounded-full bg-accent-light animate-typing-dot"
        style={{ animationDelay: '160ms' }}
      />
      <span
        className="h-1.5 w-1.5 rounded-full bg-accent-light animate-typing-dot"
        style={{ animationDelay: '320ms' }}
      />
    </span>
  )
}

function streamingStatus(message: Message): { label: string; detail?: string } | null {
  if (!message.streaming) return null
  const tools = message.toolCalls ?? []
  const pending = tools.filter((t) => t.pending)

  if (pending.length > 0) {
    return {
      label: pending.length === 1 ? 'Running tool' : `Running ${pending.length} tools`,
      detail: pending.map((t) => t.tool).join(', '),
    }
  }

  const activeStep = message.stepProgress?.find((s) => s.status === 'active')
  if (activeStep) {
    const stepIndex = (message.stepProgress?.indexOf(activeStep) ?? 0) + 1
    const total = message.stepProgress?.length ?? 1
    return { label: `Executing step ${stepIndex}/${total}`, detail: activeStep.description }
  }

  const retryingStep = message.stepProgress?.find((s) => s.status === 'retrying')
  if (retryingStep) {
    return { label: 'Retrying step', detail: retryingStep.description }
  }

  if (message.plan && !message.content.trim()) {
    return { label: 'Planning', detail: 'Breaking the task into executable steps' }
  }

  if (!message.content.trim()) {
    return { label: 'Thinking', detail: 'Waiting for the first response token' }
  }

  return null
}

function formatResponseDuration(durationMs: number): string {
  if (!Number.isFinite(durationMs) || durationMs < 0) return ''
  if (durationMs < 1000) return `${Math.max(0.1, durationMs / 1000).toFixed(1)}s`
  if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`

  const totalSeconds = Math.round(durationMs / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`
}

export const MessageBubble = memo(function MessageBubble({
  message,
  agentName = DEFAULT_AGENT_NAME,
}: {
  message: Message
  agentName?: string
}) {
  const isUser = message.role === 'user'
  const assistantLabel = resolveAgentName(agentName)

  if (isUser) {
    return (
      <div className="mb-5 flex justify-end animate-slide-up">
        <div className="max-w-[78%] rounded-2xl rounded-tr-md bg-accent px-4 py-3 text-sm leading-relaxed text-white shadow-lg shadow-accent/10">
          <div>{message.content}</div>
          <AttachmentStrip attachments={message.attachments} compact />
        </div>
      </div>
    )
  }

  const status = streamingStatus(message)
  const hasBody = message.content.trim().length > 0
  const formattedContent = useMemo(
    () => (hasBody ? formatAgentResponse(message.content) : ''),
    [hasBody, message.content],
  )
  const showToolCalls = message.toolCalls && message.toolCalls.length > 0
  const showThinking = !message.streaming && isDisplayableThinking(message.thinking)
  const showPlan = message.stepProgress && message.stepProgress.length > 0
  const runStatus = message.streaming ? 'streaming' : message.runStatus ?? 'complete'
  const activityItems = message.activityItems ?? (
    message.streaming && message.toolCalls
      ? message.toolCalls.map((toolCall, index) => ({
          id: toolCall.id || `${toolCall.tool}-${index}`,
          type: 'tool' as const,
          toolCall,
        }))
      : []
  )
  const showLiveActivity = activityItems.length > 0 && (
    message.streaming || runStatus === 'paused' || runStatus === 'error'
  ) && !hasBody
  const showExecutionSummary = !showLiveActivity && !message.streaming && (showPlan || showToolCalls)
  const responseDuration = message.responseDurationMs !== undefined
    ? formatResponseDuration(message.responseDurationMs)
    : ''
  const showLiveDuration = message.streaming && message.responseStartedAtMs !== undefined
  const showLiveWorkPanel =
    (!hasBody && (status || (message.streaming && showPlan))) || showThinking
  const statusBadge =
    runStatus === 'streaming'
      ? { label: 'Streaming', className: 'border-accent/30 bg-accent/10 text-accent-light' }
      : runStatus === 'paused'
        ? { label: 'Paused', className: 'border-amber-400/25 bg-amber-400/10 text-amber-200' }
        : runStatus === 'error'
          ? { label: 'Error', className: 'border-red-400/25 bg-red-400/10 text-red-200' }
          : { label: 'Complete', className: 'border-white/[0.08] bg-white/[0.03] text-neutral-500' }

  const mascotSrc =
    runStatus === 'complete' ? mascotGood
    : runStatus === 'streaming' ? mascotThinking
    : mascotCurious

  return (
    <article className="mb-6 flex gap-3 animate-slide-up">
      <div className="mt-1 h-10 w-10 shrink-0 overflow-hidden rounded-xl">
        <img
          src={mascotSrc}
          alt="Agent mascot"
          className="h-full w-full object-contain"
          draggable={false}
        />
      </div>
      <div className="min-w-0 flex-1 space-y-3">
        <div className="flex items-center gap-2">
          <span className="max-w-[14rem] truncate text-xs font-medium text-neutral-300">
            {assistantLabel}
          </span>
          <span className={`status-pill ${statusBadge.className}`}>
            {statusBadge.label}
          </span>
        </div>

        {showLiveWorkPanel && (
          <LiveWorkPanel
            status={status}
            thinking={showThinking ? message.thinking : undefined}
            streaming={message.streaming}
            steps={message.stepProgress ?? []}
            startedAtMs={showLiveDuration ? message.responseStartedAtMs : undefined}
          />
        )}

        {showLiveActivity && (
          <ActivityTimeline items={activityItems} />
        )}

        {message.approvals && message.approvals.length > 0 && (
          <div className="space-y-2">
            {message.approvals.map((a, i) => (
              <ApprovalCard key={`${a.ticket_id}-${i}`} notice={a} />
            ))}
          </div>
        )}

        {hasBody && (
          <div className="agent-prose max-w-none text-sm leading-relaxed text-neutral-200">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                pre({ children }) {
                  return (
                    <pre className="my-4 overflow-x-auto rounded-xl border border-white/[0.08] bg-black/30 p-4">
                      {children}
                    </pre>
                  )
                },
                code({ className, children, ...props }) {
                  const isInline = !className
                  if (isInline) {
                    return (
                      <code
                        className="rounded-md border border-white/[0.08] bg-white/[0.06] px-1.5 py-0.5 font-mono text-xs code-token"
                        {...props}
                      >
                        {children}
                      </code>
                    )
                  }

                  return (
                    <code className={`font-mono text-xs text-neutral-200 ${className ?? ''}`} {...props}>
                      {children}
                    </code>
                  )
                },
                table({ children }) {
                  return (
                    <div className="my-4 overflow-x-auto rounded-xl border border-white/[0.08] bg-white/[0.025]">
                      <table className="min-w-full border-collapse text-left text-xs text-neutral-200">
                        {children}
                      </table>
                    </div>
                  )
                },
                th({ children }) {
                  return (
                    <th className="border-b border-white/[0.08] bg-white/[0.04] px-3 py-2 font-semibold text-neutral-100">
                      {children}
                    </th>
                  )
                },
                td({ children }) {
                  return (
                    <td className="border-t border-white/[0.06] px-3 py-2 align-top">
                      {children}
                    </td>
                  )
                },
                a({ children, href }) {
                  return (
                    <a href={href} target="_blank" rel="noreferrer">
                      {children}
                    </a>
                  )
                },
              }}
            >
              {formattedContent}
            </ReactMarkdown>
          </div>
        )}

        <AttachmentStrip attachments={message.attachments} />

        {showExecutionSummary && (
          <ExecutionSummary
            messageId={message.id}
            steps={message.stepProgress ?? []}
            toolCalls={message.toolCalls ?? []}
          />
        )}

        {!message.streaming && responseDuration && (
          <ResponseTimer duration={responseDuration} />
        )}
      </div>
    </article>
  )
})

function ResponseTimer({
  startedAtMs,
  duration,
}: {
  startedAtMs?: number
  duration?: string
}) {
  const [now, setNow] = useState(() => Date.now())
  const isLive = startedAtMs !== undefined

  useEffect(() => {
    if (!isLive) return
    const timer = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(timer)
  }, [isLive])

  const label = isLive
    ? `Elapsed time ${formatResponseDuration(Math.max(0, now - startedAtMs))}`
    : `Response time ${duration}`

  return (
    <div
      className="flex items-center gap-1.5 text-[11px] text-neutral-500"
      aria-label={label}
    >
      <Timer size={12} className="shrink-0 text-neutral-600" aria-hidden />
      <span>{label}</span>
    </div>
  )
}

function LiveWorkPanel({
  status,
  thinking,
  streaming,
  steps,
  startedAtMs,
}: {
  status: ReturnType<typeof streamingStatus>
  thinking?: string
  streaming?: boolean
  steps: NonNullable<Message['stepProgress']>
  startedAtMs?: number
}) {
  const hasThinking = isDisplayableThinking(thinking)
  const hasPlan = steps.length > 0
  const hasDetails = hasThinking || hasPlan
  const [open, setOpen] = useState(false)
  const completedSteps = steps.filter((step) => step.status === 'done').length
  const title = status?.label ?? (hasThinking ? 'Thinking' : 'Planning')
  const detail = status?.detail ?? (streaming ? 'Preparing next step' : undefined)

  const indicator = streaming ? (
    <TypingDots className="ml-0.5" />
  ) : (
    <ThinkingStatusIcon />
  )

  const row = (
    <div className="relative flex min-w-0 items-center gap-3 px-3 py-2">
      {indicator}
      <div className="flex min-w-0 flex-1 items-baseline gap-2">
        <span className="shrink-0 text-[12px] font-medium text-neutral-200">{title}</span>
        {detail && (
          <span className="min-w-0 flex-1 truncate text-[11px] text-neutral-500">
            <span className="mx-1.5 text-neutral-700">·</span>
            {detail}
          </span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {hasPlan && (
          <span className="font-mono text-[10px] text-neutral-500">
            {completedSteps}/{steps.length}
          </span>
        )}
        {hasThinking && (
          <span className="rounded-full border border-accent/25 bg-accent/10 px-1.5 py-px text-[9px] font-medium uppercase tracking-wider text-accent-light">
            Trace
          </span>
        )}
        {streaming && startedAtMs !== undefined && (
          <InlineElapsed startedAtMs={startedAtMs} />
        )}
        {hasDetails && (
          <span className="text-neutral-600">
            {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          </span>
        )}
      </div>
    </div>
  )

  return (
    <div
      className="live-surface relative overflow-hidden rounded-xl text-xs"
      role="status"
      aria-live="polite"
    >
      {streaming && (
        <span
          className="pointer-events-none absolute inset-x-0 bottom-0 h-px overflow-hidden"
          aria-hidden
        >
          <span className="live-shimmer absolute inset-0 animate-progress-shimmer" />
        </span>
      )}
      {hasDetails ? (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="block w-full text-left transition-colors hover:bg-white/[0.025] focus:outline-none focus:ring-2 focus:ring-accent/20"
          aria-expanded={open}
        >
          {row}
        </button>
      ) : (
        row
      )}

      {open && hasDetails && (
        <div className="space-y-3 border-t border-lime-300/[0.10] p-3">
          {hasPlan && <PlanProgress steps={steps} title="Plan" />}
          {hasThinking && (
            <section aria-label="Reasoning trace" className="rounded-lg bg-black/15 px-3 py-2">
              <p className="section-label mb-1.5 flex items-center gap-1.5">
                <BrainCircuit size={11} className="text-accent-light" aria-hidden />
                Reasoning trace
              </p>
              <p className="thinking-trace max-h-40 overflow-y-auto whitespace-pre-wrap break-words text-[11px] italic leading-relaxed text-neutral-400">
                {thinking}
              </p>
            </section>
          )}
        </div>
      )}
    </div>
  )
}

function InlineElapsed({ startedAtMs }: { startedAtMs: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(timer)
  }, [])
  const duration = formatResponseDuration(Math.max(0, now - startedAtMs))
  return (
    <span
      className="font-mono text-[10px] tabular-nums text-neutral-500"
      aria-label={`Elapsed time ${duration}`}
    >
      {duration}
    </span>
  )
}

function AttachmentStrip({
  attachments,
  compact = false,
}: {
  attachments?: UploadedAttachment[]
  compact?: boolean
}) {
  if (!attachments?.length) return null
  const images = attachments.filter(
    (attachment) => attachment.mime_type.startsWith('image/') && !hasTinyImageDimensions(attachment),
  )
  const files = attachments.filter((attachment) => !attachment.mime_type.startsWith('image/'))

  return (
    <div className={compact ? 'mt-2 space-y-2' : 'space-y-2'}>
      {images.length > 0 && (
        <div className="image-strip-scroll flex gap-2 overflow-x-auto pb-1">
          {images.map((attachment) => (
            <ImageAttachmentCard
              key={attachment.id}
              attachment={attachment}
              compact={compact}
            />
          ))}
        </div>
      )}
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {files.map((attachment) => (
            <button
              type="button"
              key={attachment.id}
              onClick={() => void downloadAttachment(attachment)}
              className={`inline-flex max-w-[240px] items-center gap-1.5 rounded-lg px-2 py-1 text-[11px] transition-colors ${
                compact
                  ? 'bg-black/15 text-white/85 hover:bg-black/25'
                  : 'border border-white/[0.08] bg-white/[0.03] text-neutral-400 hover:border-white/[0.14] hover:text-neutral-200'
              }`}
              title={attachment.path}
            >
              <FileText size={12} className="shrink-0" />
              <span className="truncate">{attachment.name}</span>
              {!compact && <Download size={11} className="shrink-0 text-neutral-600" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function hasTinyImageDimensions(attachment: UploadedAttachment): boolean {
  const width = attachment.width ?? 0
  const height = attachment.height ?? 0
  if (!width || !height) return false
  return width < MIN_IMAGE_PREVIEW_DIMENSION_PX || height < MIN_IMAGE_PREVIEW_DIMENSION_PX
}

function ImageAttachmentCard({
  attachment,
  compact,
}: {
  attachment: UploadedAttachment
  compact: boolean
}) {
  const [previewFailed, setPreviewFailed] = useState(false)
  const [tooSmall, setTooSmall] = useState(false)
  const [previewHref, setPreviewHref] = useState('')

  useEffect(() => {
    let active = true
    let objectUrl = ''
    void fetchAttachmentObjectUrl(attachment, true)
      .then((value) => {
        objectUrl = value
        if (active) setPreviewHref(value)
      })
      .catch(() => {
        if (active) setPreviewFailed(true)
      })
    return () => {
      active = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [attachment])

  if (tooSmall) return null

  return (
    <button
      type="button"
      onClick={() => void downloadAttachment(attachment)}
      className={`group block shrink-0 overflow-hidden rounded-xl border transition-colors ${
        compact
          ? 'w-36 border-white/15 bg-black/10 hover:bg-black/15'
          : 'w-44 border-white/[0.08] bg-white/[0.025] hover:border-white/[0.16] hover:bg-white/[0.04]'
      }`}
      title={attachment.path}
      aria-label={`Open image ${attachment.name}`}
    >
      <span
        className={`relative flex w-full items-center justify-center overflow-hidden bg-black/25 ${
          compact ? 'h-24' : 'h-32'
        }`}
      >
        {!previewFailed && previewHref ? (
          <img
            src={previewHref}
            alt={attachment.name}
            className="h-full w-full object-contain p-1"
            loading="lazy"
            onLoad={(event) => {
              const image = event.currentTarget
              if (
                image.naturalWidth > 0
                && image.naturalHeight > 0
                && (image.naturalWidth < MIN_IMAGE_PREVIEW_DIMENSION_PX
                  || image.naturalHeight < MIN_IMAGE_PREVIEW_DIMENSION_PX)
              ) {
                setTooSmall(true)
              }
            }}
            onError={() => setPreviewFailed(true)}
          />
        ) : previewFailed ? (
          <span className="flex h-full w-full flex-col items-center justify-center gap-2 px-4 text-center">
            <Image size={24} className={compact ? 'text-white/70' : 'text-neutral-500'} aria-hidden />
            <span className={compact ? 'text-[11px] text-white/80' : 'text-[11px] text-neutral-400'}>
              Preview unavailable
            </span>
          </span>
        ) : (
          <Loader2 size={20} className="animate-spin text-neutral-500" aria-label="Loading preview" />
        )}
      </span>
      <span
        className={`flex min-w-0 items-center gap-2 border-t px-2.5 py-2 text-[11px] ${
          compact
            ? 'border-white/15 text-white/85'
            : 'border-white/[0.08] text-neutral-400 group-hover:text-neutral-200'
        }`}
      >
        <Image size={12} className="shrink-0" aria-hidden />
        <span className="min-w-0 flex-1 truncate">{attachment.name}</span>
        {attachment.size > 0 && (
          <span className={compact ? 'shrink-0 text-white/55' : 'shrink-0 text-neutral-600'}>
            {formatBytes(attachment.size)}
          </span>
        )}
      </span>
    </button>
  )
}

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return ''
  if (size < 1024) return `${size} B`
  const units = ['KB', 'MB', 'GB']
  let value = size / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`
}

function ActivityTimeline({ items }: { items: ActivityItem[] }) {
  const progressItems = items.filter(
    (item): item is Extract<ActivityItem, { type: 'progress' }> => item.type === 'progress',
  )
  const toolCalls = items
    .filter((item): item is Extract<ActivityItem, { type: 'tool' }> => item.type === 'tool')
    .map((item) => item.toolCall)
  const latestProgress = progressItems[progressItems.length - 1]

  return (
    <div className="space-y-2" aria-live="polite">
      {latestProgress && (
        <ProgressUpdate
          content={latestProgress.content}
          count={progressItems.length}
        />
      )}
      {toolCalls.length > 0 && <ToolCallScroller toolCalls={toolCalls} />}
    </div>
  )
}

function ProgressUpdate({ content, count = 1 }: { content: string; count?: number }) {
  return (
    <div
      className="relative flex items-start gap-3 py-1 pl-3 pr-1 text-sm leading-relaxed text-neutral-200"
      role="status"
      aria-live="polite"
    >
      <span
        className="narration-accent pointer-events-none absolute inset-y-1 left-0 w-[3px] rounded-full"
        aria-hidden
      />
      <p className="min-w-0 flex-1 break-words text-[13.5px]">{content}</p>
      {count > 1 && (
        <span
          className="shrink-0 self-center rounded-full border border-accent/25 bg-accent/10 px-2 py-0.5 font-mono text-[10px] tabular-nums text-accent-light"
          aria-label={`${count} updates`}
        >
          {count}
        </span>
      )}
    </div>
  )
}

function toolCallKey(toolCall: ToolCall, index: number): string {
  return toolCall.id || `${toolCall.tool}-${index}`
}

function toolCallStatus(toolCall: ToolCall): 'running' | 'error' | 'finished' | 'waiting' {
  if (toolCall.pending === true) return 'running'
  const normalizedStatus = String(toolCall.status || '').toLowerCase()
  if (normalizedStatus === 'error') return 'error'
  if (normalizedStatus && normalizedStatus !== 'pending') return 'finished'
  const done = toolCall.output !== undefined
  if (done && (toolCall.output?.startsWith('Error:') || toolCall.output?.startsWith('error:'))) {
    return 'error'
  }
  return done ? 'finished' : 'waiting'
}

function ToolTabStatusIcon({ status }: { status: ReturnType<typeof toolCallStatus> }) {
  if (status === 'running') {
    return <Loader2 className="shrink-0 animate-spin text-accent-light" size={13} aria-hidden />
  }
  if (status === 'error') {
    return <XCircle className="shrink-0 text-red-300" size={13} aria-hidden />
  }
  if (status === 'finished') {
    return <Check className="shrink-0 text-emerald-300" size={13} aria-hidden />
  }
  return <Wrench className="shrink-0 text-neutral-500" size={13} aria-hidden />
}

function toolInputSummary(toolCall: ToolCall): string {
  const raw = toolCall.input || ''
  try {
    const parsed = JSON.parse(raw)
    if (typeof parsed === 'object' && parsed !== null) {
      const preview = parsed.url || parsed.path || parsed.query || parsed.command || parsed.code || parsed.content || parsed.selector || parsed.text || parsed.action || ''
      if (typeof preview === 'string' && preview.length > 0) {
        return preview.length > 60 ? preview.slice(0, 57) + '...' : preview
      }
    }
  } catch { /* not JSON */ }
  if (raw.length > 60) return raw.slice(0, 57) + '...'
  return raw
}

const INITIAL_VISIBLE = 3

function ToolCallScroller({
  messageId,
  toolCalls,
}: {
  messageId?: string
  toolCalls: ToolCall[]
}) {
  const [resolvedCalls, setResolvedCalls] = useState(toolCalls)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)
  const [loadingDetails, setLoadingDetails] = useState(false)
  const [detailsError, setDetailsError] = useState('')
  const detailsRequestedRef = useRef(false)
  const hasRunning = resolvedCalls.some((t) => t.pending)
  const visibleCalls = showAll ? resolvedCalls : resolvedCalls.slice(0, INITIAL_VISIBLE)
  const hiddenCount = Math.max(0, resolvedCalls.length - INITIAL_VISIBLE)
  const expandedToolCall = resolvedCalls.find((toolCall, index) => toolCallKey(toolCall, index) === expandedId)

  useEffect(() => {
    setResolvedCalls(toolCalls)
    setExpandedId(null)
    setDetailsError('')
    setLoadingDetails(false)
    detailsRequestedRef.current = false
  }, [toolCalls])

  useEffect(() => {
    if (
      messageId === undefined
      || expandedToolCall?.previewOnly !== true
      || detailsRequestedRef.current
    ) {
      return
    }
    const numericMessageId = Number(messageId)
    if (!Number.isFinite(numericMessageId)) {
      return
    }

    const abortController = new AbortController()
    let cancelled = false
    detailsRequestedRef.current = true
    setLoadingDetails(true)
    setDetailsError('')
    void fetchMessageToolCalls(numericMessageId, abortController.signal)
      .then((toolCallRows) => {
        if (cancelled) return
        setResolvedCalls(
          toolCallRows.map((toolCall) => ({
            id: String(toolCall.id),
            tool: toolCall.tool_name,
            input: toolCall.input,
            output: toolCall.output,
            pending: false,
            status: toolCall.status,
            previewOnly: false,
          })),
        )
      })
      .catch((error: unknown) => {
        if (cancelled) return
        if (error instanceof DOMException && error.name === 'AbortError') return
        detailsRequestedRef.current = false
        setDetailsError(error instanceof Error ? error.message : 'Failed to load tool details')
      })
      .finally(() => {
        if (!cancelled) setLoadingDetails(false)
      })
    return () => {
      cancelled = true
      abortController.abort()
    }
  }, [expandedToolCall?.previewOnly, messageId])

  return (
    <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-white/[0.025] text-xs">
      <div className="flex min-w-0 items-center gap-2 px-3 py-2">
        <Wrench size={13} className="shrink-0 text-neutral-400" aria-hidden />
        <span className="shrink-0 font-medium text-neutral-300">
          {hasRunning ? 'Running tools' : 'Tools'}
        </span>
        <span className="status-pill border-white/[0.08] bg-white/[0.03] text-neutral-500">
          {resolvedCalls.length}
        </span>
      </div>
      <div className="activity-tool-scroll max-h-[132px] overflow-y-auto" role="tablist" aria-label="Tool calls">
          {visibleCalls.map((toolCall, index) => {
            const key = toolCallKey(toolCall, index)
            const status = toolCallStatus(toolCall)
            const isExpanded = expandedId === key || (toolCall.pending === true)
            const summary = toolInputSummary(toolCall)

          return (
            <div key={key} className="border-t border-white/[0.06]">
              <button
                type="button"
                role="tab"
                aria-selected={isExpanded}
                onClick={() => setExpandedId(isExpanded && !toolCall.pending ? null : key)}
                className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-white/[0.035] focus:outline-none focus:ring-2 focus:ring-accent/20"
              >
                <ToolTabStatusIcon status={status} />
                <span className="shrink-0 font-mono text-[11px] font-medium text-neutral-200">
                  {toolCall.tool}
                </span>
                {summary && !isExpanded && (
                  <span className="min-w-0 flex-1 truncate text-[11px] text-neutral-500">
                    {summary}
                  </span>
                )}
                {!summary && <span className="min-w-0 flex-1" />}
                <span className="shrink-0 text-neutral-500">
                  {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                </span>
              </button>
              {isExpanded && (
                <div className="space-y-2 border-t border-white/[0.04] bg-black/15 px-3 py-2.5">
                  {toolCall.previewOnly && loadingDetails && (
                    <p className="text-[11px] italic text-neutral-500">Loading full tool details...</p>
                  )}
                  {detailsError && (
                    <p className="text-[11px] text-amber-300">{detailsError}</p>
                  )}
                  {toolCall.input && <ToolPayloadInline label="Input" value={toolCall.input} />}
                  {toolCall.output !== undefined ? (
                    <ToolPayloadInline label="Result" value={toolCall.output} />
                  ) : toolCall.pending ? (
                    <p className="text-[11px] italic text-neutral-500">Running...</p>
                  ) : null}
                </div>
              )}
            </div>
          )
        })}
      </div>
      {!showAll && hiddenCount > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="flex w-full items-center justify-center gap-1 border-t border-white/[0.06] px-3 py-2 text-[11px] text-neutral-500 transition-colors hover:bg-white/[0.035] hover:text-neutral-300"
        >
          Show {hiddenCount} more tool{hiddenCount > 1 ? 's' : ''}
          <ChevronDown size={12} />
        </button>
      )}
    </div>
  )
}

function ToolPayloadInline({ label, value }: { label: string; value: string }) {
  const [collapsed, setCollapsed] = useState(value.length > 300)
  const displayValue = collapsed ? value.slice(0, 300) + '...' : value
  return (
    <div>
      <p className="section-label">{label}</p>
      <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-white/[0.04] bg-black/20 p-2 font-mono text-[11px] text-neutral-400">
        {displayValue}
      </pre>
      {collapsed && (
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          className="mt-1 text-[10px] text-accent-light hover:underline"
        >
          Show full output
        </button>
      )}
    </div>
  )
}

function ExecutionSummary({
  messageId,
  steps,
  toolCalls,
}: {
  messageId: string
  steps: NonNullable<Message['stepProgress']>
  toolCalls: NonNullable<Message['toolCalls']>
}) {
  const [expanded, setExpanded] = useState(false)
  const completedSteps = steps.filter((step) => step.status === 'done').length
  const executedTools = toolCalls.filter((tool) => !tool.pending).length

  return (
    <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-white/[0.025] text-xs">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-white/[0.035] focus:outline-none focus:ring-2 focus:ring-accent/20"
      >
        <span className="min-w-0 flex-1 text-neutral-300">
          Plan {completedSteps}/{steps.length || 0} done · Tools executed {executedTools}
        </span>
        <span className="text-[11px] text-neutral-500">
          {expanded ? 'Hide details' : 'Expand to see all'}
        </span>
        {expanded ? <ChevronDown size={13} className="text-neutral-500" /> : <ChevronRight size={13} className="text-neutral-500" />}
      </button>

      {expanded && (
        <div className="space-y-3 border-t border-white/[0.08] p-3">
          {steps.length > 0 && <PlanProgress steps={steps} />}
          {toolCalls.length > 0 && (
            <ToolCallScroller messageId={messageId} toolCalls={toolCalls} />
          )}
        </div>
      )}
    </div>
  )
}

function ApprovalCard({ notice }: { notice: ApprovalNotice }) {
  const [localStatus, setLocalStatus] = useState<'idle' | 'approving' | 'rejecting' | 'approved' | 'rejected'>('idle')

  const handleApprove = async () => {
    setLocalStatus('approving')
    try {
      await approveTicket(notice.ticket_id)
      setLocalStatus('approved')
    } catch {
      setLocalStatus('idle')
    }
  }

  const handleReject = async () => {
    setLocalStatus('rejecting')
    try {
      await rejectTicket(notice.ticket_id)
      setLocalStatus('rejected')
    } catch {
      setLocalStatus('idle')
    }
  }

  if (notice.type !== 'required' || localStatus === 'approved' || localStatus === 'rejected') {
    const resolved = localStatus === 'approved' || notice.type === 'resolved'
    const resumed = notice.type === 'resumed'
    const Icon = resolved ? CheckCircle2 : resumed ? Play : XCircle
    const label = resolved ? 'Approved' : resumed ? 'Executing' : 'Rejected'
    const tone = resolved || resumed
      ? 'border-emerald-400/20 bg-emerald-400/10 text-emerald-300'
      : 'border-red-400/20 bg-red-400/10 text-red-300'

    return (
      <div className={`flex items-center gap-2 rounded-xl border px-3 py-2 text-xs ${tone}`}>
        <Icon size={14} className="shrink-0" />
        <span className="font-medium">{label}</span>
        <span className="min-w-0 truncate text-neutral-400">{notice.action}</span>
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-amber-400/25 bg-amber-400/10 p-3">
      <div className="mb-3 flex items-start gap-3">
        <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-300" />
        <div className="min-w-0 flex-1">
          <p className="text-xs font-semibold text-amber-200">Approval required</p>
          <p className="mt-1 text-[11px] leading-relaxed text-neutral-200">{notice.action}</p>
          {notice.reason && (
            <p className="mt-1 text-[10px] text-neutral-500">{notice.reason}</p>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2 pl-7">
        <button
          type="button"
          onClick={handleApprove}
          disabled={localStatus !== 'idle'}
          className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-emerald-400 disabled:opacity-50"
        >
          {localStatus === 'approving' ? <CircleDashed size={12} className="animate-spin" /> : <Check size={12} />}
          {localStatus === 'approving' ? 'Approving' : 'Approve'}
        </button>
        <button
          type="button"
          onClick={handleReject}
          disabled={localStatus !== 'idle'}
          className="ghost-button rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50"
        >
          {localStatus === 'rejecting' ? <CircleDashed size={12} className="animate-spin" /> : <X size={12} />}
          {localStatus === 'rejecting' ? 'Rejecting' : 'Reject'}
        </button>
      </div>
    </div>
  )
}
