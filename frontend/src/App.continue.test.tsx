import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import type { Conversation } from './lib/api/types'

const mocks = vi.hoisted(() => ({
  chat: {
    messages: [] as unknown[],
    isStreaming: false,
    isLoadingHistory: false,
    isLoadingOlderHistory: false,
    hasMoreHistory: false,
    pendingAccessGrant: null,
    setPendingAccessGrant: vi.fn(),
    loadOlderMessages: vi.fn(),
    sendMessage: vi.fn(),
    steerMessage: vi.fn(),
    steeringConversationId: null,
    queued: [],
    queuePaused: false,
    queueMessage: vi.fn(),
    removeQueued: vi.fn(),
    refreshMessages: vi.fn(),
    stopStreaming: vi.fn(),
    clearMessages: vi.fn(),
  },
  runController: {
    runs: [] as unknown[],
    snapshot: null as unknown,
    operations: [],
    recovery: null,
    loading: false,
    error: null,
    eventsConnected: true,
    refreshing: false,
    refreshLatest: vi.fn(),
    selectRun: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    stop: vi.fn(),
    verifyOperation: vi.fn(),
    skipOperation: vi.fn(),
    refreshRun: vi.fn(),
  },
  conversations: [] as Conversation[],
  fetchConversations: vi.fn(),
  fetchScheduledTasks: vi.fn(),
}))

vi.mock('./components/AppBar', () => ({ AppBar: () => null }))
vi.mock('./components/Sidebar', () => ({ Sidebar: (props: any) => (
  <button onClick={() => props.onSelect('chat-1')}>Open saved chat</button>
) }))
vi.mock('./components/ChatWindow', () => ({ ChatWindow: () => null }))
vi.mock('./components/InputBar', () => ({ InputBar: () => null }))
vi.mock('./components/ApprovalToast', () => ({ ApprovalToast: () => null }))
vi.mock('./components/AccessGrantCard', () => ({ AccessGrantCard: () => null }))
vi.mock('./components/ExecutionTimeline', () => ({ ExecutionTimeline: (props: any) => (
  <button onClick={() => void props.onAction('resume')}>Continue saved Run</button>
) }))
vi.mock('./hooks/useChat', () => ({ useChat: () => mocks.chat }))
vi.mock('./hooks/useRunController', () => ({ useRunController: () => mocks.runController }))
vi.mock('./hooks/useItemActions', () => ({ useItemActions: () => ({ feedback: {}, perform: vi.fn() }) }))
vi.mock('./lib/api/conversations', () => ({
  fetchConversations: () => mocks.fetchConversations(),
  deleteConversation: vi.fn(),
  renameConversation: vi.fn(),
  setConversationWorkspace: vi.fn(),
  validateConversationWorkspace: vi.fn(),
}))
vi.mock('./lib/api/scheduledTasks', () => ({
  fetchScheduledTasks: () => mocks.fetchScheduledTasks(),
  deleteScheduledTask: vi.fn(),
  runScheduledTaskNow: vi.fn(),
  updateScheduledTask: vi.fn(),
}))
vi.mock('./lib/api/settings', () => ({
  fetchModelOptions: vi.fn().mockRejectedValue(new Error('No model options in App test')),
  fetchSettings: vi.fn().mockResolvedValue({
    permissions: { mode: 'default' },
    llm: { provider: 'openai', model_name: 'fixture', reasoning_effort: 'low' },
    api_keys: { has_google_key: false },
    identity: { agent_name: 'Monaw' },
  }),
  updateSettings: vi.fn(),
}))
vi.mock('./lib/apiKeySync', () => ({ syncStoredApiKeysToBackendWithRetry: vi.fn().mockResolvedValue(true) }))
vi.mock('./lib/api/serverEvents', () => ({ subscribeServerEvents: vi.fn(() => () => {}) }))
vi.mock('./lib/api/accessGrants', () => ({ fetchPendingAccessGrants: vi.fn().mockResolvedValue([]) }))
vi.mock('./features/onboarding/useSetupReadiness', () => ({ useSetupReadiness: () => ({}) }))
vi.mock('./features/onboarding/readiness', () => ({ shouldShowGuide: () => false }))
vi.mock('./features/onboarding/FirstRunGuide', () => ({ FirstRunGuide: () => null }))

beforeEach(() => {
  Object.assign(mocks.chat, { isStreaming: false })
  mocks.chat.refreshMessages.mockReset()
  mocks.conversations = [{
    id: 'chat-1', title: 'Saved chat', created_at: '2026-11-04T00:00:00Z', updated_at: '2026-11-04T00:00:00Z', workspace_dir: '',
  }]
  mocks.fetchConversations.mockResolvedValue(mocks.conversations)
  mocks.fetchScheduledTasks.mockResolvedValue([])
  Object.assign(mocks.runController, {
    snapshot: { run_id: 'run-1', conversation_id: 'chat-1', status: 'paused' },
  })
  mocks.runController.refreshLatest.mockReset()
  mocks.runController.resume.mockReset().mockImplementation(async () => {
    mocks.runController.snapshot = { run_id: 'run-1', conversation_id: 'chat-1', status: 'running' }
    return { accepted: true, run: mocks.runController.snapshot }
  })
})

afterEach(() => cleanup())

it('keeps the Continue message refresh pending until an active chat stream ends', async () => {
  const app = render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Open saved chat' }))
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Continue saved Run' }))
  })
  expect(mocks.runController.resume).toHaveBeenCalledOnce()

  mocks.chat.isStreaming = true
  mocks.runController.snapshot = { run_id: 'run-1', conversation_id: 'chat-1', status: 'completed' }
  app.rerender(<App />)
  expect(mocks.chat.refreshMessages).not.toHaveBeenCalled()

  mocks.chat.isStreaming = false
  app.rerender(<App />)
  await waitFor(() => expect(mocks.chat.refreshMessages).toHaveBeenCalledOnce())
})
