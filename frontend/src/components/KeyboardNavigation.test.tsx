import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Conversation, ScheduledTask } from '../lib/api/types'
import { ScheduledTaskItem } from './scheduling/ScheduledTaskItem'
import { Sidebar } from './Sidebar'

afterEach(cleanup)

const historyRow = () => screen.getByTitle('Double-click or press F2 to rename')

function renderSidebar(overrides: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const props = {
    conversations: [
      { id: 'c1', title: 'Quarterly report', created_at: new Date().toISOString() } as Conversation,
    ],
    activeId: null,
    collapsed: false,
    onToggleCollapsed: vi.fn(),
    onSelect: vi.fn(),
    onDelete: vi.fn(),
    onRename: vi.fn().mockResolvedValue(true),
    onOpenSettings: vi.fn(),
    scheduledTasks: [],
    onOpenScheduledTaskCreate: vi.fn(),
    onOpenScheduledTaskEdit: vi.fn(),
    onSelectScheduledRun: vi.fn(),
    onToggleScheduledTaskEnabled: vi.fn(),
    onDeleteScheduledTask: vi.fn(),
    onRunScheduledTask: vi.fn(),
    theme: 'dark' as const,
    onToggleTheme: vi.fn(),
    ...overrides,
  }
  render(<Sidebar {...props} />)
  return props
}

describe('conversation history from the keyboard', () => {
  it('opens a conversation with a real button and renames it with F2', async () => {
    const props = renderSidebar()
    const row = historyRow()

    fireEvent.click(row)
    expect(props.onSelect).toHaveBeenCalledWith('c1')

    fireEvent.keyDown(row, { key: 'F2' })
    const input = await screen.findByRole('textbox', { name: 'Rename Quarterly report' })
    fireEvent.change(input, { target: { value: 'Q3 report' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(props.onRename).toHaveBeenCalledWith('c1', 'Q3 report'))
  })

  it('returns focus to the row when a rename is cancelled with Escape', async () => {
    renderSidebar()
    fireEvent.keyDown(historyRow(), { key: 'F2' })
    const input = await screen.findByRole('textbox', { name: 'Rename Quarterly report' })

    fireEvent.keyDown(input, { key: 'Escape' })

    await waitFor(() => expect(historyRow()).toHaveFocus())
  })

  it('asks before deleting, starting on Cancel, and Escape keeps the conversation', () => {
    const props = renderSidebar()
    fireEvent.click(screen.getByRole('button', { name: 'Delete Quarterly report' }))

    const prompt = screen.getByRole('group', { name: 'Delete "Quarterly report"?' })
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus()
    fireEvent.keyDown(prompt, { key: 'Escape' })

    expect(props.onDelete).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Delete Quarterly report' })).toHaveFocus()

    fireEvent.click(screen.getByRole('button', { name: 'Delete Quarterly report' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(props.onDelete).toHaveBeenCalledWith('c1')
  })
})

describe('scheduled task actions from the keyboard', () => {
  const task = {
    id: 't1', title: 'Report', prompt: 'Work', scheduleKind: 'interval', intervalSeconds: 60, enabled: true,
    lastRunStatus: '', lastRunConversationId: '', nextRunAt: '', running: false, queued: false,
  } as unknown as ScheduledTask

  function renderItem() {
    const props = {
      task, onEdit: vi.fn(), onSelectRun: vi.fn(), onToggleEnabled: vi.fn(), onDelete: vi.fn(), onRunNow: vi.fn(), onViewRuns: vi.fn(),
    }
    render(<ScheduledTaskItem {...props} />)
    return props
  }

  it('moves through the menu with arrows and closes on Escape back to its button', () => {
    renderItem()
    const trigger = screen.getByRole('button', { name: 'Scheduled task actions for Report' })
    fireEvent.click(trigger)
    const menu = screen.getByRole('menu', { name: 'Actions for Report' })

    expect(screen.getByRole('menuitem', { name: 'Edit' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(screen.getByRole('menuitem', { name: 'Run now' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'End' })
    expect(screen.getByRole('menuitem', { name: 'Delete' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(screen.getByRole('menuitem', { name: 'Edit' })).toHaveFocus()

    fireEvent.keyDown(menu, { key: 'Escape' })
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  it('confirms before deleting a task', () => {
    const props = renderItem()
    fireEvent.click(screen.getByRole('button', { name: 'Scheduled task actions for Report' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete' }))

    expect(props.onDelete).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    expect(props.onDelete).toHaveBeenCalledWith('t1')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
