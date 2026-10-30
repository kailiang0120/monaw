import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ScheduledTask } from '../../lib/api/types'
import { ScheduledRunHistory } from './ScheduledRunHistory'
import { ScheduledTaskDialog } from './ScheduledTaskDialog'

vi.mock('../../lib/api/scheduledTasks', () => ({
  createScheduledTask: vi.fn(),
  updateScheduledTask: vi.fn(),
  runScheduledTaskNow: vi.fn(),
  fetchTelegramChats: vi.fn().mockResolvedValue([]),
  fetchScheduledTaskRuns: vi.fn().mockResolvedValue([]),
  previewSchedule: vi.fn().mockResolvedValue({ next: [] }),
}))

const escape = () => fireEvent.keyDown(document.activeElement ?? window, { key: 'Escape' })

function renderDialog() {
  const onClose = vi.fn()
  render(<ScheduledTaskDialog task={null} onClose={onClose} onSaved={() => {}} onRunConversation={() => {}} />)
  return { onClose }
}

describe('ScheduledTaskDialog keyboard use', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(cleanup)

  it('opens with focus inside the dialog and closes on Escape when nothing changed', () => {
    const { onClose } = renderDialog()

    expect(screen.getByRole('dialog', { name: 'New scheduled task' }).contains(document.activeElement)).toBe(true)
    escape()

    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('asks before discarding edits, and Escape at the prompt keeps editing', () => {
    const { onClose } = renderDialog()
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Morning briefing' } })

    escape()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByText('Close and lose your changes to this task?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Keep editing' })).toHaveFocus()

    escape()
    expect(screen.queryByText('Close and lose your changes to this task?')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Title')).toHaveValue('Morning briefing')

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes only the timezone list when Escape is pressed in its search', async () => {
    const { onClose } = renderDialog()
    fireEvent.click(screen.getByRole('button', { name: /At set times/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Timezone' }))
    const search = await screen.findByLabelText('Search timezone')
    await waitFor(() => expect(search).toHaveFocus())

    fireEvent.keyDown(search, { key: 'Escape' })

    expect(screen.queryByLabelText('Search timezone')).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.queryByText('Close and lose your changes to this task?')).not.toBeInTheDocument()
  })
})

describe('ScheduledRunHistory keyboard use', () => {
  afterEach(cleanup)

  it('closes on Escape', () => {
    const onClose = vi.fn()
    render(
      <ScheduledRunHistory
        task={{ id: 'task-1', title: 'Morning briefing' } as ScheduledTask}
        onClose={onClose}
        onSelectRun={() => {}}
      />,
    )

    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()
    escape()

    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
