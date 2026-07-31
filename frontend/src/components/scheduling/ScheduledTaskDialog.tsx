import { useEffect, useMemo, useState } from 'react'
import { AlertCircle, Clock, Play, X } from 'lucide-react'
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
import {
  ChoiceCard,
  Note,
  SettingsCard,
  StackedRow,
  SwitchRow,
} from '../ui/Panel'
import { Dropdown } from '../Dropdown'
import { ScheduleEditor } from './ScheduleEditor'

/**
 * "Skip / Queue / Replace" tells you the mechanism but not the consequence.
 * Each option says what actually happens to the run you care about.
 */
const OVERLAP_OPTIONS: Array<{
  value: ScheduledTaskOverlapPolicy
  label: string
  summary: string
}> = [
  {
    value: 'skip',
    label: 'Skip the new run',
    summary: 'Let the current run finish and wait for the next scheduled time.',
  },
  {
    value: 'queue',
    label: 'Wait in line',
    summary: 'Start the new run as soon as the current one finishes.',
  },
  {
    value: 'cancel_previous',
    label: 'Cancel and restart',
    summary: 'Stop the run in progress and start the new one immediately.',
  },
]

interface Props {
  task?: ScheduledTask | null
  onClose: () => void
  onSaved: () => void
  onRunConversation: (conversationId: string) => void
}

export function ScheduledTaskDialog({
  task,
  onClose,
  onSaved,
  onRunConversation,
}: Props) {
  const [title, setTitle] = useState(task?.title ?? '')
  const [prompt, setPrompt] = useState(task?.prompt ?? '')
  const [scheduleKind, setScheduleKind] = useState<ScheduleKind>(task?.scheduleKind ?? 'interval')
  const [cronExpr, setCronExpr] = useState(task?.cronExpr ?? '0 8 * * 1-5')
  const [intervalSeconds, setIntervalSecs] = useState(task?.intervalSeconds ?? 3600)
  const [runAt, setRunAt] = useState(toLocalDateTimeInput(task?.runAt))
  const [timezone, setTimezone] = useState(
    task?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'UTC',
  )
  const [enabled, setEnabled] = useState(task?.enabled ?? true)
  const [notifyTelegram, setNotifyTelegram] = useState(task?.notifyTelegram ?? false)
  const [telegramChatId, setTelegramChatId] = useState(task?.telegramChatId ?? '')
  const [telegramChats, setTelegramChats] = useState<TelegramChatTarget[]>([])
  const [reuseConversation, setReuseConversation] = useState(task?.reuseConversation ?? false)
  const [overlapPolicy, setOverlapPolicy] = useState<ScheduledTaskOverlapPolicy>(
    task?.overlapPolicy ?? 'skip',
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  // Auto-fill the title from the first line of the prompt, until it is edited.
  useEffect(() => {
    if (!title.trim() && prompt.trim()) {
      setTitle(prompt.trim().replace(/\s+/g, ' ').slice(0, 60))
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prompt])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
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
    () => Boolean(title.trim() && prompt.trim() && !saving),
    [title, prompt, saving],
  )

  const buildInput = (): ScheduledTaskInput => ({
    title: title.trim(),
    prompt: prompt.trim(),
    scheduleKind,
    cronExpr: scheduleKind === 'cron' ? cronExpr.trim() : '',
    intervalSeconds: scheduleKind === 'interval' ? intervalSeconds : 0,
    runAt: scheduleKind === 'once' ? runAt : '',
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
      // A failed lookup just leaves the field for manual entry.
    }
  }

  return (
    <div className="st-overlay fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={task ? 'Edit scheduled task' : 'New scheduled task'}
        className="settings-shell flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl"
      >
        <header className="st-divider-b flex items-center justify-between gap-3 px-5 py-4">
          <div className="flex items-center gap-3">
            <div
              className="flex h-9 w-9 items-center justify-center rounded-lg"
              style={{ background: 'var(--st-accent-soft)', color: 'var(--st-accent-text)' }}
            >
              <Clock size={16} />
            </div>
            <div>
              <h2 className="st-title">{task ? 'Edit scheduled task' : 'New scheduled task'}</h2>
              <p className="st-desc mt-0.5">
                Monaw will send the prompt below to the agent on its own, on the schedule you set.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="st-btn st-btn-ghost st-btn-icon shrink-0"
            aria-label="Close"
          >
            <X size={15} />
          </button>
        </header>

        <div className="st-scroll flex-1 space-y-4 overflow-y-auto px-5 py-5">
          <SettingsCard title="What to run">
            <StackedRow label="Title" description="Shown in the sidebar so you can find this task later.">
              <input
                value={title}
                aria-label="Title"
                onChange={(e) => setTitle(e.target.value)}
                className="st-input"
                placeholder="Morning briefing"
              />
            </StackedRow>
            <StackedRow
              label="Prompt"
              description="Sent to the agent word for word on every run. Write it the way you would type it into the chat box."
            >
              <textarea
                value={prompt}
                aria-label="Prompt"
                onChange={(e) => setPrompt(e.target.value)}
                rows={5}
                className="st-input min-h-[100px] resize-y font-mono leading-relaxed"
                placeholder="Summarise my unread emails and list today's calendar."
              />
            </StackedRow>
          </SettingsCard>

          <SettingsCard title="When to run">
            <div className="p-4">
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
          </SettingsCard>

          <SettingsCard title="Where the results go">
            <SwitchRow
              label="Keep all runs in one conversation"
              description="Every run continues the same chat, so the agent can see what happened last time. Off means each run starts fresh with no memory of previous runs."
              checked={reuseConversation}
              onChange={setReuseConversation}
            />
            <SwitchRow
              label="Send the result to Telegram"
              description="Also delivers each finished run to a Telegram chat. Needs a bot token saved under Settings → Connections."
              checked={notifyTelegram}
              onChange={setNotifyTelegram}
            />
            {notifyTelegram && (
              <StackedRow
                label="Telegram chat"
                description="Which conversation the result is delivered to."
                action={
                  <button type="button" onClick={fillTelegramChat} className="st-btn st-btn-ghost">
                    Use most recent
                  </button>
                }
              >
                {telegramChats.length > 0 ? (
                  <Dropdown
                    ariaLabel="Telegram chat"
                    value={telegramChatId}
                    onChange={setTelegramChatId}
                    options={telegramChats.map((chat) => ({ value: chat.id, label: chat.label }))}
                  />
                ) : (
                  <input
                    value={telegramChatId}
                    aria-label="Telegram chat"
                    onChange={(e) => setTelegramChatId(e.target.value)}
                    className="st-input"
                    placeholder="Telegram chat ID"
                  />
                )}
              </StackedRow>
            )}
          </SettingsCard>

          <SettingsCard title="Options">
            <SwitchRow
              label="Task is active"
              description="Turn this off to pause the schedule without deleting the task."
              checked={enabled}
              onChange={setEnabled}
            />
            <StackedRow
              label="If the previous run has not finished"
              description="Long tasks can still be running when the next one is due. This decides what happens."
            >
              <div className="grid gap-2 sm:grid-cols-3">
                {OVERLAP_OPTIONS.map(({ value, label, summary }) => (
                  <ChoiceCard
                    key={value}
                    ariaLabel={label}
                    title={label}
                    summary={summary}
                    selected={overlapPolicy === value}
                    onSelect={() => setOverlapPolicy(value)}
                  />
                ))}
              </div>
            </StackedRow>
          </SettingsCard>

          {error && <Note tone="danger" icon={AlertCircle}>{error}</Note>}
        </div>

        <footer className="st-divider-t flex items-center justify-between gap-2 px-5 py-3.5">
          <div>
            {task && (
              <button type="button" onClick={runNow} disabled={saving} className="st-btn st-btn-secondary">
                <Play size={13} />
                Run now
              </button>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={onClose} className="st-btn st-btn-ghost">
              Cancel
            </button>
            <button type="button" onClick={save} disabled={!canSave} className="st-btn st-btn-primary">
              {saving ? 'Saving…' : task ? 'Update task' : 'Create task'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}

function toLocalDateTimeInput(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const offset = date.getTimezoneOffset() * 60_000
  return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}
