import { useEffect, useRef, useState } from 'react'
import { Activity, Loader2, RefreshCw, ShieldQuestion, Wrench } from 'lucide-react'
import { MessageBubble } from './MessageBubble'
import type { Message } from '../hooks/useChat'
import { DEFAULT_AGENT_NAME, resolveAgentName } from '../lib/identity'
import { getSessionInsights } from '../lib/sessionInsights'
import startMascotImg from '../assets/mascots/start.png'

function LivePulseDot() {
  return (
    <span className="relative flex h-2 w-2 shrink-0 items-center justify-center" aria-hidden>
      <span className="absolute h-2 w-2 animate-ping rounded-full bg-accent-light/55" />
      <span className="relative h-1.5 w-1.5 rounded-full bg-accent-light shadow-[0_0_8px_rgba(200,240,154,0.6)]" />
    </span>
  )
}

interface Props {
  messages: Message[]
  isLoadingHistory?: boolean
  conversationTitle?: string
  isStreaming?: boolean
  agentName?: string
  onRename?: (title: string) => void
  onRefresh?: () => void
  onPromptSelect?: (prompt: string) => void // kept for API compat
}

export function ChatWindow({
  messages,
  isLoadingHistory,
  conversationTitle,
  isStreaming = false,
  agentName = DEFAULT_AGENT_NAME,
  onRename,
  onRefresh,
}: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const titleInputRef = useRef<HTMLInputElement>(null)
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleValue, setTitleValue] = useState('')
  const insights = getSessionInsights(messages)
  const { metrics } = insights
  const assistantLabel = resolveAgentName(agentName)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const startTitleEdit = () => {
    if (!conversationTitle || !onRename) return
    setTitleValue(conversationTitle)
    setEditingTitle(true)
    setTimeout(() => titleInputRef.current?.select(), 0)
  }

  const commitTitleEdit = () => {
    const trimmed = titleValue.trim()
    if (trimmed && onRename) onRename(trimmed)
    setEditingTitle(false)
  }

  const cancelTitleEdit = () => setEditingTitle(false)

  return (
    <section className="relative flex min-h-0 flex-1 flex-col bg-[#11100f]">
      <header className="drag-region border-b border-white/[0.07] bg-[#11100f]/95 px-5 py-4">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
          
            <div className="no-drag min-w-0">
              <p className="section-label">Conversation</p>
              {editingTitle ? (
                <input
                  ref={titleInputRef}
                  value={titleValue}
                  onChange={(e) => setTitleValue(e.target.value)}
                  onBlur={commitTitleEdit}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') { e.preventDefault(); commitTitleEdit() }
                    if (e.key === 'Escape') { e.preventDefault(); cancelTitleEdit() }
                  }}
                  className="mt-1 w-full max-w-xs rounded bg-white/[0.06] px-1.5 py-0.5 text-base font-semibold tracking-tight text-neutral-100 outline-none ring-1 ring-accent/50"
                />
              ) : (
                <h1
                  className={`mt-1 truncate text-base font-semibold tracking-tight text-neutral-100 ${conversationTitle && onRename ? 'cursor-text hover:text-neutral-200' : ''}`}
                  title={conversationTitle && onRename ? 'Click to rename' : undefined}
                  onClick={startTitleEdit}
                >
                  {conversationTitle || 'New chat'}
                </h1>
              )}
            </div>
          </div>
          <div className="no-drag flex flex-wrap items-center justify-end gap-2">
            <button
              type="button"
              onClick={onRefresh}
              disabled={!onRefresh || isLoadingHistory || isStreaming}
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.03] text-neutral-500 outline-none transition-colors hover:border-white/[0.12] hover:text-neutral-200 focus:border-accent/50 disabled:cursor-not-allowed disabled:opacity-50"
              aria-label="Refresh chat"
              title="Refresh chat"
            >
              <RefreshCw size={13} className={isLoadingHistory ? 'animate-spin' : ''} />
            </button>
            <HeaderMetric icon={Wrench} label="Tools" value={metrics.toolCalls} />
            <HeaderMetric icon={Activity} label="Running" value={metrics.pendingTools} />
            <HeaderMetric icon={ShieldQuestion} label="Approvals" value={metrics.approvals} />
            <span
              className={`status-pill ${
                isStreaming
                  ? 'border-accent/30 bg-accent/10 text-accent-light'
                  : 'border-white/[0.07] bg-white/[0.03] text-neutral-500'
              }`}
            >
              {isStreaming ? 'Streaming' : 'Ready'}
            </span>
          </div>
        </div>
      </header>

      {isLoadingHistory ? (
        <div className="flex flex-1 select-none flex-col items-center justify-center gap-3 text-neutral-500">
          <Loader2 size={24} className="animate-spin text-neutral-500" />
          <p className="text-sm font-medium">Loading conversation</p>
        </div>
      ) : messages.length === 0 ? (
        <div className="flex flex-1 select-none flex-col items-center justify-center gap-5 px-4">
          <img
            src={startMascotImg}
            alt={assistantLabel}
            className="h-28 w-28 animate-idle-float object-contain drop-shadow-[0_8px_24px_rgba(79,122,43,0.18)]"
            draggable={false}
          />
          <div className="text-center">
            <p className="text-base font-medium tracking-tight text-neutral-400">{assistantLabel}</p>
            <p className="mt-0.5 text-[13px] text-neutral-600">Ready.</p>
          </div>
        </div>
      ) : (
        <div className={`flex-1 overflow-y-auto px-4 pt-6 ${isStreaming ? 'pb-28' : 'pb-6'}`}>
          <div className="mx-auto max-w-3xl">
            {messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} agentName={assistantLabel} />
            ))}
            <div ref={bottomRef} />
          </div>
        </div>
      )}

      {isStreaming && (
        <div className="pointer-events-none absolute inset-x-4 bottom-4 z-20">
          <div className="pointer-events-auto mx-auto max-w-3xl">
            <LiveRunDock message={insights.latestAssistant} />
          </div>
        </div>
      )}
    </section>
  )
}

function LiveRunDock({
  message,
}: {
  message?: Message
}) {
  const steps = message?.stepProgress ?? []
  const activeStep = steps.find((step) => step.status === 'active' || step.status === 'retrying')
  const completedSteps = steps.filter((step) => step.status === 'done').length
  const runningTool = [...(message?.toolCalls ?? [])].reverse().find((tool) => tool.pending)

  const primary = runningTool
    ? 'Running tool'
    : activeStep?.description
      ? `Step ${(steps.indexOf(activeStep) + 1)}/${steps.length}`
      : steps.length
        ? 'Preparing next step'
        : 'Planning response'

  const secondary = runningTool
    ? runningTool.tool
    : activeStep?.description

  return (
    <div
      className="live-surface relative flex items-center gap-2.5 overflow-hidden rounded-full py-1.5 pl-3 pr-2 shadow-[0_12px_36px_-12px_rgba(0,0,0,0.45)] backdrop-blur-md"
      role="status"
      aria-live="polite"
    >
      <LivePulseDot />
      <span className="shrink-0 text-[11px] font-medium text-neutral-200">{primary}</span>
      {secondary && (
        <>
          <span className="shrink-0 text-neutral-600" aria-hidden>·</span>
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-accent-light">
            {secondary}
          </span>
        </>
      )}
      {!secondary && <span className="min-w-0 flex-1" />}
      {steps.length > 0 && (
        <span className="shrink-0 rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 font-mono text-[10px] tabular-nums text-accent-light">
          {completedSteps}/{steps.length}
        </span>
      )}
      <span
        className="pointer-events-none absolute inset-x-0 bottom-0 h-px overflow-hidden"
        aria-hidden
      >
        <span className="live-shimmer absolute inset-0 animate-progress-shimmer" />
      </span>
    </div>
  )
}

function HeaderMetric({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Activity
  label: string
  value: number
}) {
  if (value === 0) return null
  return (
    <div className="hidden animate-fade-in h-7 items-center gap-1.5 rounded-lg border border-accent/20 bg-accent/10 px-2.5 text-[10px] text-accent-light sm:inline-flex">
      <Icon size={11} className="text-accent-light/70" />
      <span>{label}</span>
      <span className="font-semibold">{value}</span>
    </div>
  )
}
