import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ConversationHistoryDialog } from './ConversationHistoryDialog'
import { fetchConversations } from '../lib/api/conversations'
import type { Conversation } from '../lib/api/types'

vi.mock('../lib/api/conversations', () => ({ fetchConversations: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal')
  Reflect.deleteProperty(HTMLDialogElement.prototype, 'close')
})

it('pages through old chats and searches the full history before reopening a result', async () => {
  // jsdom does not implement the browser's dialog top layer.
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: {
      configurable: true,
      value: function (this: HTMLDialogElement) {
        // Expose the dialog to role queries as a real showModal call would.
        this.setAttribute('open', '')
      },
    },
    close: {
      configurable: true,
      value: function (this: HTMLDialogElement) {
        // Notify React through the native close event.
        this.removeAttribute('open')
        this.dispatchEvent(new Event('close'))
      },
    },
  })
  const recent = Array.from({ length: 51 }, (_, index) => ({
    id: `chat-${index}`, title: `Chat ${index}`, created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-26T00:00:00Z',
  })) as Conversation[]
  const oldChat = { id: 'old-chat', title: 'Archived plan', created_at: '2025-01-01T00:00:00Z' } as Conversation
  vi.mocked(fetchConversations)
    .mockResolvedValueOnce(recent)
    .mockResolvedValueOnce([oldChat])
    .mockResolvedValueOnce([oldChat])
  const onSelect = vi.fn()
  const onClose = vi.fn()
  render(<ConversationHistoryDialog onSelect={onSelect} onClose={onClose} />)
  expect(screen.getByLabelText('Search all chats')).toHaveFocus()

  fireEvent.click(await screen.findByRole('button', { name: 'Load older chats' }))
  await screen.findByText('Archived plan')
  expect(fetchConversations).toHaveBeenNthCalledWith(2, 51, 0, expect.objectContaining({
    beforeId: 'chat-49', beforeUpdatedAt: recent[49].updated_at,
  }))

  fireEvent.change(screen.getByLabelText('Search all chats'), { target: { value: 'plan' } })
  await waitFor(() => expect(fetchConversations).toHaveBeenNthCalledWith(3, 51, 0, expect.objectContaining({
    search: 'plan', beforeId: undefined,
  })))
  fireEvent.click(await screen.findByRole('button', { name: /Archived plan/ }))
  expect(onSelect).toHaveBeenCalledWith(oldChat)
  expect(onClose).toHaveBeenCalledOnce()
})
