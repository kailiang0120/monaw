import { memo, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  AlertTriangle,
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
  SquareTerminal,
  Wrench,
  X,
  XCircle,
} from 'lucide-react'
import mascotAlert from '../assets/mascots/alert.gif'
import mascotCurious from '../assets/mascots/curious.gif'
import mascotSuccess from '../assets/mascots/success.gif'
import mascotThinking from '../assets/mascots/thinking.gif'
import mascotAlertStill from '../assets/mascots/curious.png'
import mascotSuccessStill from '../assets/mascots/good.png'
import mascotThinkingStill from '../assets/mascots/thinking.png'
import { PlanProgress } from './PlanProgress'
import { useVisibleInterval } from '../hooks/useVisibleInterval'
import { approveTicket, rejectTicket } from '../lib/api/approvals'
import { fetchMessageToolCalls } from '../lib/api/conversations'
import { downloadAttachment, fetchAttachmentObjectUrl } from '../lib/api/files'
import { formatAgentResponse } from '../lib/formatAgentResponse'
import { DEFAULT_AGENT_NAME, resolveAgentName } from '../lib/identity'
import type { UploadedAttachment } from '../lib/api/types'
import type { ActivityItem, ApprovalNotice, Message, ToolCall } from '../hooks/useChat'

const MIN_IMAGE_PREVIEW_DIMENSION_PX = 16
const FORMATTED_RESPONSE_CACHE_LIMIT = 300
const formattedResponseCache = new Map<string, string>()

type DisplayActivity =
  | { id: string; type: 'progress'; content: string }
  | { id: string; type: 'tools'; toolCalls: ToolCall[] }

function cachedFormatAgentResponse(message: Message): string {
  const revision = message.contentRevision ?? message.content.length
  const key = `${message.id}:${revision}`
  const cached = formattedResponseCache.get(key)
  if (cached !== undefined) return cached
  const formatted = formatAgentResponse(message.content)
  formattedResponseCache.set(key, formatted)
  if (formattedResponseCache.size > FORMATTED_RESPONSE_CACHE_LIMIT) {
    const firstKey = formattedResponseCache.keys().next().value
    if (firstKey) formattedResponseCache.delete(firstKey)
  }
  return formatted
}

function formatResponseDuration(durationMs: number): string {
  if (!Number.isFinite(durationMs) || durationMs < 0) return ''
  if (durationMs < 1000) return `${Math.max(0.1, durationMs / 1000).toFixed(1)}s`
  if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(1)}s`

  const totalSeconds = durationMs / 1000
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = (totalSeconds % 60).toFixed(1)
  return `${minutes}m ${seconds}s`
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
      <div className="mb-2 flex justify-end animate-slide-up">
        <div className="max-w-[78%] rounded-2xl rounded-tr-md bg-accent px-3.5 py-2 text-sm leading-relaxed text-white shadow-lg shadow-accent/10">
          <div>{message.content}</div>
          <AttachmentStrip attachments={message.attachments} compact />
          {message.steeringStatus && (
            <p className="mt-1 text-[10px] text-white/70">
              {message.steeringStatus === 'applied' ? 'Applied to the running task'
                : message.steeringStatus === 'queued' ? 'Waiting for the next step' : 'Saved for the next turn'}
            </p>
          )}
        </div>
      </div>
    )
  }

  const hasBody = message.content.trim().length > 0
  const formattedContent = useMemo(
    () => (hasBody ? cachedFormatAgentResponse(message) : ''),
    [hasBody, message],
  )
  const steps = message.stepProgress ?? []
  const runStatus = message.streaming ? 'streaming' : message.runStatus ?? 'complete'
  const mascotSrc = runStatus === 'streaming' ? mascotThinking
    : runStatus === 'complete' ? mascotSuccess
      : runStatus === 'error' ? mascotAlert : mascotCurious
  const mascotStillSrc = runStatus === 'streaming' ? mascotThinkingStill
    : runStatus === 'complete' ? mascotSuccessStill : mascotAlertStill
  const activityItems: ActivityItem[] = message.activityItems ?? message.toolCalls?.map((toolCall, index) => ({
    id: toolCall.id || `${toolCall.tool}-${index}`,
    type: 'tool' as const,
    toolCall,
  })) ?? []
  const displayActivities = groupDisplayActivities(activityItems, message.content)

  return (
    <article className="mb-5 space-y-2.5" aria-label={`${assistantLabel} response`}>
      <div className="flex items-center gap-2 border-b border-white/[0.08] pb-1.5">
        <picture className="h-7 w-7 shrink-0" >
          <source media="(prefers-reduced-motion: reduce)" srcSet={mascotStillSrc} />
          <img src={mascotSrc} alt={`${assistantLabel} mascot`} className="h-full w-full object-contain" draggable={false} />
        </picture>
        {(message.streaming || message.responseDurationMs !== undefined) ? (
          <WorkDuration
            streaming={message.streaming === true}
            startedAtMs={message.responseStartedAtMs}
            durationMs={message.responseDurationMs}
          />
        ) : <span className="text-xs text-neutral-500">{assistantLabel}</span>}
      </div>

      {displayActivities.map((item) => item.type === 'progress'
        ? <p key={item.id} className="text-sm leading-relaxed text-neutral-300" role="status" aria-live="polite">{item.content}</p>
        : <ToolActivityGroup key={item.id} messageId={message.id} toolCalls={item.toolCalls} streaming={message.streaming === true} />)}

      {steps.length > 0 && <PlanActivity steps={steps} />}

      {(runStatus === 'paused' || runStatus === 'error') && (
        <p className={runStatus === 'error' ? 'text-xs text-red-300' : 'text-xs text-amber-300'}>
          {runStatus === 'error' ? 'Stopped with an error' : 'Paused'}
        </p>
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
    </article>
  )
})

function groupDisplayActivities(activityItems: ActivityItem[], answer: string): DisplayActivity[] {
  const groups: DisplayActivity[] = []
  for (const item of activityItems) {
    if (item.type === 'progress') {
      const content = item.content.trim()
      if (content && !answer.trim().startsWith(content)) {
        groups.push({ id: item.id, type: 'progress', content })
      }
      continue
    }
    const previous = groups[groups.length - 1]
    if (previous?.type === 'tools') {
      previous.toolCalls.push(item.toolCall)
    } else {
      groups.push({ id: item.id, type: 'tools', toolCalls: [item.toolCall] })
    }
  }
  return groups
}

function WorkDuration({
  streaming,
  startedAtMs,
  durationMs,
}: {
  streaming: boolean
  startedAtMs?: number
  durationMs?: number
}) {
  const [now, setNow] = useState(() => Date.now())
  useVisibleInterval(() => setNow(Date.now()), streaming && startedAtMs !== undefined ? 100 : null)
  const duration = streaming && startedAtMs !== undefined
    ? formatResponseDuration(Math.max(0, now - startedAtMs))
    : durationMs !== undefined ? formatResponseDuration(durationMs) : ''
  const label = `${streaming ? 'Working' : 'Worked'}${duration ? ` for ${duration}` : ''}`

  return (
    <div className="text-xs text-neutral-500" role="status" aria-live="off">
      {label}
    </div>
  )
}

function PlanActivity({ steps }: { steps: NonNullable<Message['stepProgress']> }) {
  const [expanded, setExpanded] = useState(false)
  const activeStep = steps.find((step) => step.status === 'active' || step.status === 'retrying')
  const completed = steps.filter((step) => step.status === 'done').length
  const label = activeStep?.description || `Plan · ${completed}/${steps.length} done`

  return (
    <div className="text-xs">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full min-w-0 items-center gap-2 rounded-md py-1 text-left text-neutral-400 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/30"
      >
        <CircleDashed size={14} className="shrink-0" aria-hidden />
        <span className="min-w-0 flex-1 truncate">{label}</span>
        {expanded ? <ChevronDown size={13} aria-hidden /> : <ChevronRight size={13} aria-hidden />}
      </button>
      {expanded && <div className="ml-5 pt-2"><PlanProgress steps={steps} title="Plan" /></div>}
    </div>
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
        else URL.revokeObjectURL(value)
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
      title="Download image"
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
    </button>
  )
}

function ToolActivityGroup({
  messageId,
  toolCalls,
  streaming,
}: {
  messageId: string
  toolCalls: ToolCall[]
  streaming: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const pendingTool = toolCalls.find((call) => call.pending)
  const allCommands = toolCalls.every((call) => /^exec(?:_|$)/.test(call.tool))
  const allBrowser = toolCalls.every((call) =>
    call.tool.startsWith('browser_') || call.tool.startsWith('mcp__Chrome-dev-tools__'))
  const label = pendingTool
    ? toolCallActivityLabel(pendingTool)
    : allCommands ? `Ran ${toolCalls.length === 1 ? 'command' : 'commands'}`
      : allBrowser ? 'Used browser'
        : `Used ${toolCalls.length === 1 ? 'tool' : 'tools'}`

  useEffect(() => {
    if (streaming && pendingTool) setExpanded(true)
  }, [streaming, Boolean(pendingTool)])

  return (
    <div className="text-xs">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full min-w-0 items-center gap-2 rounded-md py-1 text-left text-neutral-400 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/30"
      >
        {allCommands ? <SquareTerminal size={14} className="shrink-0" aria-hidden />
          : <Wrench size={14} className="shrink-0" aria-hidden />}
        <span className="min-w-0 flex-1 truncate" title={label}>{label}</span>
        {expanded ? <ChevronDown size={13} className="shrink-0" aria-hidden />
          : <ChevronRight size={13} className="shrink-0" aria-hidden />}
      </button>
      {expanded && (
        <div className="mt-1">
          <ToolCallScroller messageId={messageId} toolCalls={toolCalls} />
        </div>
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
      const preview = parsed.command || parsed.cmd || parsed.code || parsed.url || parsed.path || parsed.query || parsed.content || parsed.selector || parsed.text || parsed.action || ''
      if (typeof preview === 'string' && preview.length > 0) {
        return preview.replace(/\s+/g, ' ').trim()
      }
    }
  } catch { /* not JSON */ }
  return raw.length > 180 ? `${raw.slice(0, 177)}...` : raw
}

function toolCallActivityLabel(toolCall: ToolCall): string {
  const summary = toolInputSummary(toolCall)
  const status = toolCallStatus(toolCall)
  if (/^exec(?:_|$)/.test(toolCall.tool) && summary && summary !== '{}') {
    const verb = status === 'running' ? 'Running' : status === 'error' ? 'Failed' : 'Ran'
    return `${verb} ${summary}`
  }
  const verb = status === 'running' ? 'Using' : status === 'error' ? 'Failed' : 'Used'
  return `${verb} ${toolCall.tool}${summary && summary !== '{}' ? ` · ${summary}` : ''}`
}

function ToolCallScroller({
  messageId,
  toolCalls,
}: {
  messageId?: string
  toolCalls: ToolCall[]
}) {
  const [resolvedCalls, setResolvedCalls] = useState(toolCalls)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [loadingDetails, setLoadingDetails] = useState(false)
  const [detailsError, setDetailsError] = useState('')
  const detailsRequestedRef = useRef(false)
  const expandedToolCall = resolvedCalls.find((toolCall, index) => toolCallKey(toolCall, index) === expandedId)

  useEffect(() => {
    setResolvedCalls((previous) => {
      const loadedDetails = new Map(previous.flatMap((call, index) =>
        call.previewOnly === false ? [[toolCallKey(call, index), call] as const] : [],
      ))
      return toolCalls.map((call, index) =>
        call.previewOnly ? loadedDetails.get(toolCallKey(call, index)) ?? call : call,
      )
    })
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
    <ol className="space-y-1" aria-label="Tool calls">
      {resolvedCalls.map((toolCall, index) => {
        const key = toolCallKey(toolCall, index)
        const status = toolCallStatus(toolCall)
        const isExpanded = expandedId === key
        const label = toolCallActivityLabel(toolCall)

        return (
            <li
              key={key}
              className={`min-w-0 overflow-hidden rounded-lg border ${isExpanded ? 'mb-2 border-white/[0.06] bg-black/15' : 'border-transparent'}`}
            >
              <button
                type="button"
                aria-expanded={isExpanded}
                onClick={() => setExpandedId(isExpanded ? null : key)}
                className="flex w-full min-w-0 items-center gap-2 px-2 py-1.5 text-left transition-colors hover:bg-white/[0.04] focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/30"
                title={label}
              >
                {/^(?:exec|exec_)/.test(toolCall.tool)
                  ? <SquareTerminal size={13} className="shrink-0 text-neutral-500" aria-hidden />
                  : <Wrench size={13} className="shrink-0 text-neutral-500" aria-hidden />}
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-neutral-400">{label}</span>
                <ToolTabStatusIcon status={status} />
                <span className="shrink-0 text-neutral-600" aria-hidden>
                  {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                </span>
              </button>
              {isExpanded && (
                <div className="space-y-3 border-t border-white/[0.06] px-2.5 py-2.5">
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
            </li>
        )
      })}
    </ol>
  )
}

function ToolPayloadInline({ label, value }: { label: string; value: string }) {
  const [showFull, setShowFull] = useState(false)
  const formattedValue = useMemo(() => formatToolPayload(value), [value])
  const truncated = !showFull && formattedValue.length > 800
  const displayValue = truncated ? `${formattedValue.slice(0, 800)}…` : formattedValue
  return (
    <div>
      <p className="section-label">{label}</p>
      <pre className="mt-1 whitespace-pre-wrap break-words rounded-lg border border-white/[0.04] bg-black/20 p-3 font-mono text-xs leading-5 text-neutral-400">
        {displayValue}
      </pre>
      {truncated && (
        <button
          type="button"
          onClick={() => setShowFull(true)}
          className="mt-1 text-xs text-accent-light hover:underline"
        >
          {label === 'Input' ? 'Show full input' : 'Show full result'}
        </button>
      )}
    </div>
  )
}

function formatToolPayload(value: string): string {
  const trimmed = value.trim()
  if (trimmed.length > 50_000 || (!trimmed.startsWith('{') && !trimmed.startsWith('['))) {
    return value
  }
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2)
  } catch {
    return value
  }
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
