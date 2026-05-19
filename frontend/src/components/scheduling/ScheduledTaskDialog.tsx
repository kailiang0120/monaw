import { useEffect, useMemo, useState } from 'react'
import { Check, ChevronDown, Clock, MessageSquare, Play, Send, X } from 'lucide-react'
import {
  createScheduledTask,
  fetchTelegramChats,
  runScheduledTaskNow,
  updateScheduledTask,
} from '../../lib/api/scheduledTasks'
import type {
  ScheduleKind,
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskOverlapPolicy,
  TelegramChatTarget,
} from '../../lib/api/types'
import { Dropdown } from '../Dropdown'
import { ScheduleEditor } from './ScheduleEditor'

// ─── sub-components ───────────────────────────────────────────────────────────

function Toggle({
  checked,
  onChange,
}: {
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-8 shrink-0 cursor-pointer items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-accent/30 ${
        checked ? 'bg-accent' : 'bg-white/[0.12]'
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform ${
          checked ? 'translate-x-[14px]' : 'translate-x-[3px]'
        }`}
      />
    </button>
  )
}

const OVERLAP_OPTIONS: {
  value: ScheduledTaskOverlapPolicy
  label: string
  desc: string
}[] = [
  { value: 'skip',            label: 'Skip',    desc: 'Ignore new fire while running' },
  { value: 'queue',           label: 'Queue',   desc: 'Wait for current run to finish' },
  { value: 'cancel_previous', label: 'Replace', desc: 'Cancel current, start fresh' },
]

// ─── props ────────────────────────────────────────────────────────────────────

interface Props {
  task?: ScheduledTask | null
  onClose: () => void
  onSaved: () => void
  onRunConversation: (conversationId: string) => void
}

// ─── main component ───────────────────────────────────────────────────────────

export function ScheduledTaskDialog({
  task,
  onClose,
  onSaved,
  onRunConversation,
}: Props) {
  const [title, setTitle]                   = useState(task?.title ?? '')
  const [prompt, setPrompt]                 = useState(task?.prompt ?? '')
  const [scheduleKind, setScheduleKind]     = useState<ScheduleKind>(task?.scheduleKind ?? 'interval')
  const [cronExpr, setCronExpr]             = useState(task?.cronExpr ?? '0 8 * * 1-5')
  const [intervalSeconds, setIntervalSecs]  = useState(task?.intervalSeconds ?? 3600)
  const [runAt, setRunAt]                   = useState(toLocalDateTimeInput(task?.runAt))
  const [timezone, setTimezone]             = useState(
    task?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'UTC',
  )
  const [enabled, setEnabled]               = useState(task?.enabled ?? true)
  const [notifyTelegram, setNotifyTelegram] = useState(task?.notifyTelegram ?? false)
  const [telegramChatId, setTelegramChatId] = useState(task?.telegramChatId ?? '')
  const [telegramChats, setTelegramChats]   = useState<TelegramChatTarget[]>([])
  const [reuseConversation, setReuseConversation] = useState(task?.reuseConversation ?? false)
  const [overlapPolicy, setOverlapPolicy]   = useState<ScheduledTaskOverlapPolicy>(
    task?.overlapPolicy ?? 'skip',
  )
  const [advancedOpen, setAdvancedOpen]     = useState(false)
  const [saving, setSaving]                 = useState(false)
  const [error, setError]                   = useState('')

  // Auto-fill title from first line of prompt
  useEffect(() => {
    if (!title.trim() && prompt.trim()) {
      setTitle(prompt.trim().replace(/\s+/g, ' ').slice(0, 60))
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prompt])

  // Escape key to close
  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', fn)
    return () => document.removeEventListener('keydown', fn)
  }, [onClose])

  useEffect(() => {
    if (!notifyTelegram) return
    let cancelled = false
    fetchTelegramChats()
      .then((chats) => {
        if (cancelled) return
        setTelegramChats(chats)
        if (chats[0]) setTelegramChatId((current) => current.trim() ? current : chats[0].id)
      })
      .catch(() => {
        if (!cancelled) setTelegramChats([])
      })
    return () => {
      cancelled = true
    }
  }, [notifyTelegram])

  const canSave = useMemo(
    () => title.trim() && prompt.trim() && !saving,
    [title, prompt, saving],
  )

  const buildInput = (): ScheduledTaskInput => ({
    title: title.trim(),
    prompt: prompt.trim(),
    scheduleKind,
    cronExpr:        scheduleKind === 'cron'     ? cronExpr.trim() : '',
    intervalSeconds: scheduleKind === 'interval' ? intervalSeconds : 0,
    runAt:           scheduleKind === 'once'     ? runAt : '',
    timezone,
    enabled,
    overlapPolicy,
    notifyTelegram,
    telegramChatId: notifyTelegram ? telegramChatId.trim() : '',
    reuseConversation,
  })

  const save = async () => {
    if (!canSave) return
    setSaving(true)
    setError('')
    try {
      if (task) await updateScheduledTask(task.id, buildInput())
      else await createScheduledTask(buildInput())
      onSaved()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save scheduled task')
    } finally {
      setSaving(false)
    }
  }

  const runNow = async () => {
    if (!task) return
    setSaving(true)
    setError('')
    try {
      const result = await runScheduledTaskNow(task.id)
      onSaved()
      onRunConversation(result.conversationId)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to run scheduled task')
    } finally {
      setSaving(false)
    }
  }

  const fillTelegramChat = async () => {
    try {
      const chats = await fetchTelegramChats()
      setTelegramChats(chats)
      if (chats[0]) setTelegramChatId(chats[0].id)
    } catch {
      // ignore
    }
  }

  return (
    /* Overlay — no backdrop-click-to-close; use X or Cancel instead */
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
      {/* Panel */}
      <div className="panel flex max-h-[92vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl">

        {/* ── Header ─────────────────────────────────────── */}
        <div className="flex items-center justify-between border-b border-white/[0.07] px-5 py-4">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/20">
              <Clock size={15} className="text-accent-light" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-neutral-100">
                {task ? 'Edit scheduled task' : 'New scheduled task'}
              </h2>
              <p className="text-[11px] text-neutral-500">
                {reuseConversation ? 'Runs reuse one conversation' : 'Runs create normal conversations'}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="ghost-button h-8 w-8 rounded-lg"
            aria-label="Close"
          >
            <X size={15} />
          </button>
        </div>

        {/* ── Scrollable body ─────────────────────────────── */}
        <div className="space-y-5 overflow-y-auto px-5 py-5">

          {/* Title */}
          <div className="space-y-1.5">
            <label className="block text-xs font-medium text-neutral-400">Title</label>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="control w-full rounded-lg px-3 py-2 text-sm"
              placeholder="Morning briefing"
            />
          </div>

          {/* Prompt */}
          <div className="space-y-1.5">
            <label className="block text-xs font-medium text-neutral-400">Prompt</label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={5}
              className="control min-h-[100px] w-full resize-y rounded-lg px-3 py-2 font-mono text-sm leading-relaxed"
              placeholder="Summarize my unread emails and list today's calendar."
            />
          </div>

          {/* Schedule */}
          <div className="space-y-1.5">
            <span className="block text-xs font-medium text-neutral-400">Schedule</span>
            <ScheduleEditor
              scheduleKind={scheduleKind}
              cronExpr={cronExpr}
              intervalSeconds={intervalSeconds}
              runAt={runAt}
              timezone={timezone}
              onScheduleKindChange={setScheduleKind}
              onCronExprChange={setCronExpr}
              onIntervalSecondsChange={setIntervalSecs}
              onRunAtChange={setRunAt}
              onTimezoneChange={setTimezone}
            />
          </div>

          {/* ── Delivery section ─────────────────────────── */}
          <div className="flex items-center gap-3 py-1">
            <div className="h-px flex-1 bg-white/[0.06]" />
            <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-neutral-600">
              Delivery
            </span>
            <div className="h-px flex-1 bg-white/[0.06]" />
          </div>

          <div className="space-y-3 rounded-xl border border-white/[0.07] bg-white/[0.02] px-4 py-3">
            {/* Chat mode */}
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm text-neutral-300">
                {reuseConversation ? (
                  <span className="flex h-4 w-4 items-center justify-center rounded-sm bg-accent/20">
                    <Check size={10} className="text-accent-light" />
                  </span>
                ) : (
                  <MessageSquare size={13} className="text-neutral-500" />
                )}
                Reuse chat
              </div>
              <Toggle checked={reuseConversation} onChange={setReuseConversation} />
            </div>

            {/* Telegram toggle */}
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm text-neutral-300">
                <Send size={13} className="text-neutral-500" />
                Send to Telegram
              </div>
              <Toggle checked={notifyTelegram} onChange={setNotifyTelegram} />
            </div>

            {/* Telegram target (conditional) */}
            {notifyTelegram && (
              <div className="flex gap-2 pt-0.5">
                {telegramChats.length > 0 ? (
                  <select
                    value={telegramChatId}
                    onChange={(e) => setTelegramChatId(e.target.value)}
                    className="control min-w-0 flex-1 rounded-lg px-3 py-2 text-sm"
                    aria-label="Telegram chat"
                  >
                    {telegramChats.map((chat) => (
                      <option key={chat.id} value={chat.id}>
                        {chat.label}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    value={telegramChatId}
                    onChange={(e) => setTelegramChatId(e.target.value)}
                    className="control min-w-0 flex-1 rounded-lg px-3 py-2 text-sm"
                    placeholder="Telegram chat"
                  />
                )}
                <button
                  type="button"
                  onClick={fillTelegramChat}
                  className="ghost-button rounded-lg px-3 text-xs"
                >
                  Use last
                </button>
              </div>
            )}
          </div>

          {/* ── Advanced ─────────────────────────────────── */}
          <button
            type="button"
            onClick={() => setAdvancedOpen((v) => !v)}
            className="flex items-center gap-1.5 text-xs font-medium text-neutral-500 transition-colors hover:text-neutral-300"
          >
            <ChevronDown
              size={12}
              className={`transition-transform duration-150 ${advancedOpen ? 'rotate-180' : ''}`}
            />
            {advancedOpen ? 'Hide advanced' : 'Advanced settings'}
          </button>

          {advancedOpen && (
            <div className="space-y-4 rounded-xl border border-white/[0.08] bg-black/10 px-4 py-4">
              {/* Overlap policy */}
              <div className="space-y-2">
                <p className="text-xs font-medium text-neutral-400">
                  When a run is still in progress
                </p>
                <div className="grid grid-cols-3 gap-1.5">
                  {OVERLAP_OPTIONS.map(({ value, label, desc }) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setOverlapPolicy(value)}
                      className={`rounded-lg border px-3 py-2 text-left transition-all ${
                        overlapPolicy === value
                          ? 'border-accent/50 bg-accent/10 text-accent-light'
                          : 'border-white/[0.07] text-neutral-500 hover:border-white/[0.15] hover:text-neutral-300'
                      }`}
                    >
                      <p className="text-xs font-semibold">{label}</p>
                      <p className="mt-0.5 text-[10px] leading-tight opacity-70">{desc}</p>
                    </button>
                  ))}
                </div>
              </div>

              {/* Enabled */}
              <div className="flex items-center justify-between">
                <span className="text-sm text-neutral-300">Active (enabled)</span>
                <Toggle checked={enabled} onChange={setEnabled} />
              </div>
            </div>
          )}

          {/* Error */}
          {error && (
            <p className="rounded-lg border border-red-400/20 bg-red-400/10 px-3 py-2 text-sm text-red-300">
              {error}
            </p>
          )}
        </div>

        {/* ── Footer ─────────────────────────────────────── */}
        <div className="flex items-center justify-between gap-2 border-t border-white/[0.07] px-5 py-4">
          <div>
            {task && (
              <button
                type="button"
                onClick={runNow}
                disabled={saving}
                className="ghost-button h-9 gap-1.5 rounded-lg px-3 text-sm"
              >
                <Play size={13} />
                Run now
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="ghost-button h-9 rounded-lg px-4 text-sm"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={save}
              disabled={!canSave}
              className="primary-button h-9 rounded-lg px-4 text-sm"
            >
              {saving ? 'Saving…' : task ? 'Update' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── helper ───────────────────────────────────────────────────────────────────
function toLocalDateTimeInput(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}
