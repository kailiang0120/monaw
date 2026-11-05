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
    onNewChat: vi.fn(),
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

describe('new chat', () => {
  it('starts from the sidebar, open or collapsed', () => {
    const props = renderSidebar()
    fireEvent.click(screen.getAllByRole('button', { name: 'New chat' }).find((button) => button.tabIndex === 0)!)
    expect(props.onNewChat).toHaveBeenCalledTimes(1)
  })

  it('places New chat between scheduled tasks and chat history', () => {
    renderSidebar()
    const button = screen.getAllByRole('button', { name: 'New chat' }).find((item) => item.tabIndex === 0)!
    const scheduled = screen.getByText('Scheduled')
    const conversation = historyRow()
    expect(scheduled.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(button.compareDocumentPosition(conversation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

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

describe('chat list by workspace', () => {
  const at = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString()
  const chat = (id: string, workspace: string, minutesAgo: number, extra: Partial<Conversation> = {}) => ({
    id, title: `Chat ${id}`, created_at: at(minutesAgo), updated_at: at(minutesAgo), workspace_dir: workspace, ...extra,
  }) as Conversation

  afterEach(() => { try { window.localStorage.clear() } catch { /* unavailable */ } })

  it('stays by date while every chat shares one workspace', () => {
    renderSidebar({ conversations: [chat('a', '', 1), chat('b', '', 2)] })
    expect(screen.getByText('Today')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Default workspace/ })).not.toBeInTheDocument()
  })

  it('groups by workspace, newest project first, with a new chat per group', () => {
    const conversations = [
      chat('old', 'C:\\Projects\\alpha', 90),
      chat('def', '', 30),
      chat('new', 'D:/work/beta/', 5),
    ]
    const props = renderSidebar({ conversations })
    const headers = screen.getAllByRole('button', { expanded: true }).map((button) => button.textContent)
    expect(headers).toEqual(['Scheduled0', 'beta1', 'Default workspace1', 'alpha1'])
    expect(screen.getByRole('button', { name: /^beta/ })).toHaveAttribute('title', 'D:/work/beta/')
    expect(screen.queryByText('Today')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'New chat in alpha' }))
    expect(props.onNewChat).toHaveBeenCalledWith('C:\\Projects\\alpha')
  })

  it('shows five chats per group, keeps the open and running ones, and remembers folding', () => {
    const many = Array.from({ length: 8 }, (_, index) => chat(`m${index}`, 'C:\\big', index + 1, index === 6 ? { running: true } : {}))
    renderSidebar({ conversations: [...many, chat('other', 'C:\\small', 50)], activeId: 'm7' })
    // m0-m4, plus the running m6 and the open m7.
    expect(screen.getAllByText(/^Chat m/)).toHaveLength(7)
    fireEvent.click(screen.getByRole('button', { name: 'Show 1 more' }))
    expect(screen.getAllByText(/^Chat m/)).toHaveLength(8)
    fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }))

    fireEvent.click(screen.getByRole('button', { name: /^small/ }))
    expect(screen.queryByText('Chat other')).not.toBeInTheDocument()
    cleanup()
    renderSidebar({ conversations: [...many, chat('other', 'C:\\small', 50)] })
    expect(screen.queryByText('Chat other')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^small/ })).toHaveAttribute('aria-expanded', 'false')
  })
})
