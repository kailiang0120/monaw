import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AppBar } from './AppBar'
import { ScheduledTaskItem } from './scheduling/ScheduledTaskItem'
import type { ScheduledTask } from '../lib/api/types'

afterEach(cleanup)

it('keeps a failed rename draft open so it can be corrected or retried', async () => {
  const rename = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)
  render(<AppBar sidebarCollapsed={false} onToggleSidebar={vi.fn()} onNewChat={vi.fn()} conversationTitle="Original"
    onRenameConversation={rename} status="ready" approvalMode="default" renameFeedback={{ error: 'Could not save title' }} />)
  fireEvent.click(screen.getByRole('heading', { name: 'Original' }))
  const input = screen.getByRole('textbox', { name: 'Conversation title' })
  fireEvent.change(input, { target: { value: 'My draft' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  await waitFor(() => expect(rename).toHaveBeenCalledWith('My draft'))
  expect(screen.getByRole('textbox')).toHaveValue('My draft')
  expect(screen.getByRole('alert')).toHaveTextContent('Could not save title')
  fireEvent.keyDown(input, { key: 'Enter' })
  await waitFor(() => expect(screen.queryByRole('textbox')).not.toBeInTheDocument())
})

it('shows pending operations, blocks mutation controls and offers a check for uncertain runs', () => {
  const task: ScheduledTask = {
    id: 'one', title: 'Report', prompt: 'Work', scheduleKind: 'interval', intervalSeconds: 60,
    cronExpr: '', runAt: '', timezone: 'UTC', enabled: true, overlapPolicy: 'queue', notifyTelegram: false,
    telegramChatId: '', reuseConversation: false, lastRunAt: '', lastRunStatus: '', lastRunConversationId: '',
    nextRunAt: '', consecutiveFailures: 0, running: false, queued: true, createdAt: '', updatedAt: '',
  }
  const retry = vi.fn()
  const props = { task, onEdit: vi.fn(), onSelectRun: vi.fn(), onToggleEnabled: vi.fn(), onDelete: vi.fn(), onRunNow: vi.fn(), onViewRuns: vi.fn() }
  const { rerender } = render(<ScheduledTaskItem {...props} feedback={{ pending: 'Starting or checking run' }} />)
  fireEvent.click(screen.getByRole('button', { name: 'Scheduled task actions for Report' }))
  expect(screen.getByRole('menuitem', { name: 'Run now' })).toBeDisabled()
  expect(screen.getByRole('menuitem', { name: 'Delete' })).toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent('Starting or checking run')
  expect(screen.getByText('Waiting to run')).toBeInTheDocument()
  rerender(<ScheduledTaskItem {...props} feedback={{ error: 'Response interrupted', retry, retryLabel: 'Check run' }} />)
  fireEvent.click(screen.getByRole('button', { name: 'Check run' }))
  expect(retry).toHaveBeenCalledOnce()
})
