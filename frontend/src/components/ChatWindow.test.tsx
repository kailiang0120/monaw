import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatWindow } from './ChatWindow'
import type { Message } from '../hooks/useChat'

function makeMessages(count: number): Message[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `message-${index}`,
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `Transcript message ${index}`,
  }))
}

describe('ChatWindow', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders short conversations without virtualization', () => {
    render(<ChatWindow messages={makeMessages(6)} />)

    expect(screen.getByText('Transcript message 0')).toBeInTheDocument()
    expect(screen.getByText('Transcript message 5')).toBeInTheDocument()
  })

  it('virtualizes long conversations and updates the rendered window on scroll', async () => {
    render(<ChatWindow messages={makeMessages(80)} />)

    const transcript = screen.getByLabelText('Chat transcript')
    Object.defineProperty(transcript, 'clientHeight', { configurable: true, value: 480 })

    expect(screen.getByText('Transcript message 0')).toBeInTheDocument()
    expect(screen.queryByText('Transcript message 79')).not.toBeInTheDocument()

    await waitFor(() => expect(screen.getByLabelText('Conversation messages')).toBeInTheDocument())
    Object.defineProperty(transcript, 'scrollTop', { configurable: true, value: 7_000, writable: true })
    fireEvent.scroll(transcript)

    await waitFor(() => {
      expect(screen.queryByText('Transcript message 0')).not.toBeInTheDocument()
      expect(screen.getByText('Transcript message 54')).toBeInTheDocument()
    })
  })
})
