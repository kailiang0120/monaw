import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { fetchConversations } from './lib/api/conversations'
import { fetchScheduledTasks, runScheduledTaskNow, updateScheduledTask } from './lib/api/scheduledTasks'
import type { ScheduledTask } from './lib/api/types'

vi.mock('./components/AppBar', () => ({ AppBar: (props: any) => <div data-testid="active-title">{props.conversationTitle || 'New chat'}</div> }))
vi.mock('./components/Sidebar', () => ({ Sidebar: (props: any) => <div>
  {props.scheduledTasks.map((task: ScheduledTask) => <div key={task.id}>
    <span data-testid={`enabled-${task.id}`}>{String(task.enabled)}</span>
    <button onClick={() => props.onToggleScheduledTaskEnabled(task.id, false)}>Disable {task.id}</button>
    <button onClick={() => props.onRunScheduledTask(task.id)}>Run {task.id}</button>
    <span data-testid={`error-${task.id}`}>{props.actionFeedback?.[`task:${task.id}`]?.error}</span>
  </div>)}
  {props.conversations.map((conversation: any) => <button key={conversation.id} onClick={() => props.onSelect(conversation.id)}>Select {conversation.title}</button>)}
</div> }))
vi.mock('./components/ChatWindow', () => ({ ChatWindow: () => null }))
vi.mock('./components/InputBar', () => ({ InputBar: () => null }))
vi.mock('./components/ApprovalToast', () => ({ ApprovalToast: () => null }))
vi.mock('./components/AccessGrantCard', () => ({ AccessGrantCard: () => null }))
vi.mock('./hooks/useChat', () => {
  const stable = { messages: [], isStreaming: false, pendingAccessGrant: null, clearMessages: vi.fn(),
    setPendingAccessGrant: vi.fn(), refreshMessages: vi.fn(), stopStreaming: vi.fn() }
  return { useChat: () => stable }
})
vi.mock('./lib/api/conversations', () => ({ fetchConversations: vi.fn(), deleteConversation: vi.fn(), renameConversation: vi.fn() }))
vi.mock('./lib/api/scheduledTasks', () => ({ fetchScheduledTasks: vi.fn(), updateScheduledTask: vi.fn(), runScheduledTaskNow: vi.fn(), deleteScheduledTask: vi.fn() }))
vi.mock('./lib/api/settings', () => ({
  fetchModelOptions: vi.fn().mockRejectedValue(new Error('Fixture uses fallback models')),
  fetchSettings: vi.fn().mockResolvedValue({ permissions: { mode: 'default' }, llm: { provider: 'codex', model_name: 'fixture', reasoning_effort: 'low' }, api_keys: { has_google_key: false } }),
}))
vi.mock('./lib/apiKeySync', () => ({ syncStoredApiKeysToBackendWithRetry: vi.fn().mockResolvedValue(true) }))
vi.mock('./lib/api/serverEvents', () => ({ subscribeServerEvents: vi.fn(() => () => {}) }))
vi.mock('./lib/api/accessGrants', () => ({ fetchPendingAccessGrants: vi.fn().mockResolvedValue([]) }))

const task = (id: string): ScheduledTask => ({ id, title: id, prompt: 'Work', scheduleKind: 'interval', intervalSeconds: 60,
  cronExpr: '', runAt: '', timezone: 'UTC', enabled: true, overlapPolicy: 'queue', notifyTelegram: false,
  telegramChatId: '', reuseConversation: false, lastRunAt: '', lastRunStatus: '', lastRunConversationId: '',
  nextRunAt: '', consecutiveFailures: 0, running: false, createdAt: '', updatedAt: '' })

beforeEach(() => {
  vi.mocked(fetchScheduledTasks).mockResolvedValue([task('A'), task('B')])
  vi.mocked(fetchConversations).mockResolvedValue([{ id: 'other', title: 'Other chat', created_at: '2026-10-20', workspace_dir: '' }])
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('does not undo another schedule row when an overlapping mutation fails', async () => {
  let failA!: (reason: Error) => void
  let saveB!: (value: ScheduledTask) => void
  vi.mocked(updateScheduledTask).mockImplementation((id) => id === 'A'
    ? new Promise((_, reject) => { failA = reject })
    : new Promise((resolve) => { saveB = resolve }))
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Disable A' }))
  fireEvent.click(screen.getByRole('button', { name: 'Disable B' }))
  await act(async () => { saveB({ ...task('B'), enabled: false }); await Promise.resolve() })
  expect(screen.getByTestId('enabled-B')).toHaveTextContent('false')
  await act(async () => { failA(new Error('Could not disable A')); await Promise.resolve() })
  expect(screen.getByTestId('enabled-A')).toHaveTextContent('true')
  expect(screen.getByTestId('enabled-B')).toHaveTextContent('false')
  expect(screen.getByTestId('error-A')).toHaveTextContent('Could not disable A')
})

it('does not navigate away from a later user selection when run-now returns', async () => {
  let accept!: (value: { runId: number; conversationId: string }) => void
  vi.mocked(runScheduledTaskNow).mockReturnValue(new Promise((resolve) => { accept = resolve }))
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Run A' }))
  fireEvent.click(screen.getByRole('button', { name: 'Select Other chat' }))
  await waitFor(() => expect(screen.getByTestId('active-title')).toHaveTextContent('Other chat'))
  await act(async () => { accept({ runId: 1, conversationId: 'scheduled' }); await Promise.resolve() })
  expect(screen.getByTestId('active-title')).toHaveTextContent('Other chat')
})
