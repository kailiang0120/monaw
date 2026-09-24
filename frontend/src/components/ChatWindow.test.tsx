import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatWindow } from './ChatWindow'
import type { Message } from '../hooks/useChat'

class ControlledResizeObserver implements ResizeObserver {
  static contentObserver: ControlledResizeObserver | null = null
  readonly callback: ResizeObserverCallback

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
  }

  observe(target: Element) {
    if (target.classList.contains('max-w-3xl')) ControlledResizeObserver.contentObserver = this
  }

  unobserve() {}
  disconnect() {}

  trigger() {
    this.callback([], this)
  }
}

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
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders short conversations without virtualization', () => {
    render(<ChatWindow messages={makeMessages(6)} />)

    expect(screen.getByText('Transcript message 0')).toBeInTheDocument()
    expect(screen.getByText('Transcript message 5')).toBeInTheDocument()
  })

  it('follows content growth only while the transcript is near the bottom', () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('ResizeObserver', ControlledResizeObserver)
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      frames.push(callback)
      return frames.length
    })
    const initialMessages: Message[] = [
      { id: 'user-1', role: 'user', content: 'Question' },
      { id: 'assistant-1', role: 'assistant', content: 'First', streaming: true },
    ]
    const { rerender } = render(<ChatWindow messages={initialMessages} />)
    const transcript = screen.getByLabelText('Chat transcript')
    Object.defineProperties(transcript, {
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 1000, writable: true },
      scrollTop: { configurable: true, value: 800, writable: true },
    })
    fireEvent.scroll(transcript)

    const nextMessages = [...initialMessages]
    nextMessages[1] = { ...initialMessages[1], content: 'First answer token arrived.' }
    rerender(<ChatWindow messages={nextMessages} />)
    Object.defineProperty(transcript, 'scrollHeight', { configurable: true, value: 1200, writable: true })
    act(() => ControlledResizeObserver.contentObserver?.trigger())
    act(() => frames.splice(0).forEach((frame) => frame(0)))

    expect(transcript.scrollTop).toBe(1200)
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled()

    Object.defineProperty(transcript, 'scrollTop', { configurable: true, value: 500, writable: true })
    fireEvent.scroll(transcript)
    const moreMessages = [...nextMessages]
    moreMessages[1] = { ...nextMessages[1], content: 'The answer continues growing.' }
    rerender(<ChatWindow messages={moreMessages} />)
    Object.defineProperty(transcript, 'scrollHeight', { configurable: true, value: 1500, writable: true })
    act(() => ControlledResizeObserver.contentObserver?.trigger())
    act(() => frames.splice(0).forEach((frame) => frame(0)))

    expect(transcript.scrollTop).toBe(500)
  })

  it('returns to following after the reader submits a new prompt', () => {
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      frames.push(callback)
      return frames.length
    })
    const initialMessages: Message[] = [
      { id: 'old-user', role: 'user', content: 'Earlier question' },
      { id: 'old-assistant', role: 'assistant', content: 'Earlier answer' },
    ]
    const { rerender } = render(<ChatWindow messages={initialMessages} />)
    const transcript = screen.getByLabelText('Chat transcript')
    Object.defineProperties(transcript, {
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 1000, writable: true },
      scrollTop: { configurable: true, value: 400, writable: true },
    })
    fireEvent.scroll(transcript)

    rerender(
      <ChatWindow
        messages={[
          ...initialMessages,
          { id: 'new-user', role: 'user', content: 'New question' },
          { id: 'new-assistant', role: 'assistant', content: '', streaming: true },
        ]}
      />,
    )
    Object.defineProperty(transcript, 'scrollHeight', { configurable: true, value: 1400, writable: true })
    act(() => frames.splice(0).forEach((frame) => frame(0)))

    expect(transcript.scrollTop).toBe(1400)
  })

  it('starts a selected conversation at its latest message after a prior scroll-up', () => {
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      frames.push(callback)
      return frames.length
    })
    const { rerender } = render(
      <ChatWindow
        conversationId="conversation-a"
        messages={[{ id: 'a-1', role: 'assistant', content: 'Earlier message' }]}
      />,
    )
    const transcript = screen.getByLabelText('Chat transcript')
    Object.defineProperties(transcript, {
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 1000, writable: true },
      scrollTop: { configurable: true, value: 300, writable: true },
    })
    fireEvent.scroll(transcript)

    rerender(
      <ChatWindow
        conversationId="conversation-b"
        messages={[{ id: 'b-1', role: 'assistant', content: 'Latest message' }]}
      />,
    )
    Object.defineProperty(transcript, 'scrollHeight', { configurable: true, value: 1400, writable: true })
    act(() => frames.splice(0).forEach((frame) => frame(0)))

    expect(transcript.scrollTop).toBe(1400)
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
