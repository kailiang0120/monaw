import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { fetchMessageToolCalls } from '../lib/api/conversations'
import { downloadAttachment, fetchAttachmentObjectUrl } from '../lib/api/files'
import type { Message } from '../hooks/useChat'
import { MessageBubble } from './MessageBubble'

vi.mock('../lib/api/conversations', () => ({
  fetchMessageToolCalls: vi.fn(),
}))

vi.mock('../lib/api/files', () => ({
  downloadAttachment: vi.fn(),
  fetchAttachmentObjectUrl: vi.fn(() => Promise.resolve('blob:attachment-preview')),
}))

describe('MessageBubble', () => {
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('renders assistant content without evidence cards', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-1',
          role: 'assistant',
          content: 'Acme overview',
        }}
      />,
    )

    expect(screen.getByText('Acme overview')).toBeInTheDocument()
    expect(screen.getByRole('article', { name: 'Monaw response' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'Monaw mascot' })).toHaveAttribute('src', expect.stringMatching(/success\.gif/))
  })

  it('switches the mascot animation while the assistant is working', () => {
    const { rerender } = render(<MessageBubble message={{
      id: 'msg-mascot', role: 'assistant', content: '', streaming: true,
    }} />)
    expect(screen.getByRole('img', { name: 'Monaw mascot' })).toHaveAttribute('src', expect.stringMatching(/thinking\.gif/))

    rerender(<MessageBubble message={{
      id: 'msg-mascot', role: 'assistant', content: 'Done.', streaming: false,
    }} />)
    expect(screen.getByRole('img', { name: 'Monaw mascot' })).toHaveAttribute('src', expect.stringMatching(/success\.gif/))
  })

  it('uses the configured agent name for assistant messages', () => {
    render(
      <MessageBubble
        agentName="Hermes"
        message={{
          id: 'msg-1',
          role: 'assistant',
          content: 'Ready.',
        }}
      />,
    )

    expect(screen.getByRole('article', { name: 'Hermes response' })).toBeInTheDocument()
  })

  it('shows elapsed work time as a quiet line above completed activity', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-timer',
          role: 'assistant',
          content: 'Done.',
          toolCalls: [
            {
              id: 'tool-timer',
              tool: 'browser_open',
              input: '{"url":"https://www.google.com"}',
              output: 'opened',
            },
          ],
          responseDurationMs: 1530,
        }}
      />,
    )

    expect(screen.getByText('Worked for 1.5s')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Used browser' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Response time 1.5s')).not.toBeInTheDocument()
  })

  it('keeps completed replies quiet without an empty summary card', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-complete-quiet',
          role: 'assistant',
          content: 'Finished answer.',
          responseDurationMs: 2400,
        }}
      />,
    )

    expect(screen.queryByText('Complete')).not.toBeInTheDocument()
    expect(screen.getByText('Worked for 2.4s')).toBeInTheDocument()
    expect(screen.queryByText('No model summary returned')).not.toBeInTheDocument()
  })

  it('shows answer content, work state, and elapsed time while streaming', () => {
    vi.spyOn(Date, 'now').mockReturnValue(2_000)

    render(
      <MessageBubble
        message={{
          id: 'msg-elapsed',
          role: 'assistant',
          content: 'Still writing.',
          streaming: true,
          responseStartedAtMs: 470,
        }}
      />,
    )

    expect(screen.getByText('Still writing.')).toBeInTheDocument()
    expect(screen.getByText('Working for 1.5s')).toBeInTheDocument()
    expect(screen.queryByText('Writing answer')).not.toBeInTheDocument()
  })

  it('updates the work timer in tenths of a second', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(1_000))
    render(
      <MessageBubble message={{
        id: 'msg-tenths', role: 'assistant', content: '', streaming: true,
        responseStartedAtMs: 500,
      }} />,
    )

    expect(screen.getByText('Working for 0.5s')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(100))
    expect(screen.getByText('Working for 0.6s')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(20_800))
    expect(screen.getByText('Working for 21.4s')).toBeInTheDocument()
  })

  it('renders structured plain text with headings and tables', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-2',
          role: 'assistant',
          content: `Summary:

Performance
Index        Level
S&P 500      7,173.91`,
        }}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Summary' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Performance' })).toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
  })

  it('renders fenced code blocks as block code without a language tag', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-3',
          role: 'assistant',
          content: `\`\`\`
const answer = 42
\`\`\``,
        }}
      />,
    )

    expect(screen.getByText('const answer = 42')).toBeInTheDocument()
    expect(screen.getByText('const answer = 42').closest('pre')).toBeInTheDocument()
  })

  it('renders assistant links to open outside the chat window', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-link',
          role: 'assistant',
          content: '[Open website](https://example.com)',
        }}
      />,
    )

    expect(screen.getByRole('link', { name: 'Open website' })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: 'Open website' })).toHaveAttribute('rel', 'noreferrer')
  })

  it('renders assistant response attachments as authenticated files and image previews', async () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-4',
          role: 'assistant',
          content: 'Generated the files.',
          attachments: [
            {
              id: 'image-1',
              name: 'chart.png',
              path: 'C:\\tmp\\chart.png',
              mime_type: 'image/png',
              size: 3,
            },
            {
              id: 'file-1',
              name: 'report.pdf',
              path: 'C:\\tmp\\report.pdf',
              mime_type: 'application/pdf',
              size: 3,
            },
          ],
        }}
      />,
    )

    expect(await screen.findByRole('img', { name: 'chart.png' })).toHaveAttribute(
      'src',
      'blob:attachment-preview',
    )
    fireEvent.click(screen.getByRole('button', { name: /report.pdf/i }))
    expect(downloadAttachment).toHaveBeenCalledWith(expect.objectContaining({ id: 'file-1' }))
    expect(fetchAttachmentObjectUrl).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'image-1' }),
      true,
    )
  })

  it('does not render image preview cards for tiny screenshot attachments', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-4b',
          role: 'assistant',
          content: 'Captured screenshots.',
          attachments: [
            {
              id: 'tiny-image',
              name: 'browser_snapshot_bad.png',
              path: 'C:\\tmp\\browser_snapshot_bad.png',
              mime_type: 'image/png',
              size: 300,
              width: 536,
              height: 1,
            },
          ],
        }}
      />,
    )

    expect(screen.queryByRole('img', { name: 'browser_snapshot_bad.png' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /browser_snapshot_bad/i })).not.toBeInTheDocument()
  })

  it('shows progress as plain text and exposes live tool details', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-5',
          role: 'assistant',
          content: '',
          streaming: true,
          responseStartedAtMs: 470,
          activityItems: [
            { id: 'progress-1', type: 'progress', content: 'Opening the browser.' },
            {
              id: 'tool-1',
              type: 'tool',
              toolCall: {
                id: 'tool-1',
                tool: 'browser_tabs',
                input: '{"action":"list"}',
                output: 'tabs ok',
              },
            },
            { id: 'progress-2', type: 'progress', content: 'Inspecting the page.' },
            {
              id: 'tool-2',
              type: 'tool',
              toolCall: {
                id: 'tool-2',
                tool: 'browser_snapshot',
                input: '{"include_screenshot":true}',
                pending: true,
              },
            },
          ],
        }}
      />,
    )

    expect(screen.getByText('Opening the browser.')).toBeInTheDocument()
    expect(screen.getByText('Inspecting the page.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Used browser' }))
    const lists = screen.getAllByRole('list', { name: 'Tool calls' })
    expect(lists).toHaveLength(2)
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    const transcript = screen.getByRole('article', { name: 'Monaw response' }).textContent ?? ''
    expect(transcript.indexOf('Opening the browser.')).toBeLessThan(transcript.indexOf('Used browser'))
    expect(transcript.indexOf('Used browser')).toBeLessThan(transcript.indexOf('Inspecting the page.'))

    fireEvent.click(within(lists[1]).getByRole('button', { name: /Using browser_snapshot/ }))
    expect(screen.getByText(/"include_screenshot": true/)).toBeInTheDocument()

    fireEvent.click(within(lists[0]).getByRole('button', { name: /Used browser_tabs/ }))

    expect(screen.getByText('tabs ok')).toBeInTheDocument()
  })

  it('keeps every tool call available during a long run', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-recent-tools',
          role: 'assistant',
          content: '',
          streaming: true,
          toolCalls: [1, 2, 3, 4].map((index) => ({
            id: `call-${index}`,
            tool: `tool_${index}`,
            input: '{}',
            pending: index === 4,
            output: index === 4 ? undefined : 'Done',
          })),
        }}
      />,
    )

    expect(screen.getByRole('list', { name: 'Tool calls' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(4)
    const calls = screen.getByRole('list', { name: 'Tool calls' })
    expect(within(calls).getByRole('button', { name: /Used tool_1/ })).toBeInTheDocument()
    expect(within(calls).getByRole('button', { name: /Using tool_4/ })).toBeInTheDocument()
  })

  it('shows expandable command activity below plain progress', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-command-activity',
          role: 'assistant',
          content: '',
          streaming: true,
          activityItems: [
            { id: 'progress-command', type: 'progress', content: 'Assessing test behavior' },
            {
              id: 'tool-command',
              type: 'tool',
              toolCall: {
                id: 'tool-command',
                tool: 'exec',
                input: '{"command":"npm test"}',
                output: '20 tests passed',
              },
            },
          ],
        }}
      />,
    )

    expect(screen.getByText('Assessing test behavior')).toBeInTheDocument()
    const heading = screen.getByRole('button', { name: 'Ran command' })
    expect(heading).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(heading)
    const command = within(screen.getByRole('list', { name: 'Tool calls' })).getByRole('button', { name: 'Ran npm test' })
    expect(command).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(command)
    expect(screen.getByText(/"command": "npm test"/)).toBeInTheDocument()
    expect(screen.getByText('20 tests passed')).toBeInTheDocument()

    fireEvent.click(heading)
    expect(heading).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('button', { name: 'Ran npm test' })).not.toBeInTheDocument()
  })

  it('labels Chrome DevTools MCP calls as browser activity', () => {
    render(<MessageBubble message={{
      id: 'msg-mcp-browser',
      role: 'assistant',
      content: 'Page inspected.',
      toolCalls: [{
        id: 'mcp-browser-1', tool: 'mcp__Chrome-dev-tools__take_snapshot',
        input: '{}', output: 'snapshot',
      }],
    }} />)

    expect(screen.getByRole('button', { name: 'Used browser' })).toBeInTheDocument()
  })

  it('loads full tool details on demand for preview-only history rows', async () => {
    vi.mocked(fetchMessageToolCalls).mockResolvedValue([
      {
        id: 42,
        tool_name: 'browser_snapshot',
        input: '{"url":"https://example.com/very/long/path"}',
        output: 'full snapshot payload',
        status: 'complete',
        preview_only: false,
        has_full_input: false,
        has_full_output: false,
      },
    ] as any)

    const message: Message = {
      id: '42',
      role: 'assistant',
      content: 'Done.',
      toolCalls: [
        {
          id: '42',
          tool: 'browser_snapshot',
          input: '{"url":"https://example.com/preview"}',
          output: 'preview result',
          status: 'complete',
          previewOnly: true,
        },
      ],
    }
    const { rerender } = render(<MessageBubble message={message} />)

    fireEvent.click(screen.getByRole('button', { name: 'Used browser' }))
    fireEvent.click(within(screen.getByRole('list', { name: 'Tool calls' })).getByRole('button', { name: /browser_snapshot/ }))

    expect(await screen.findByText('full snapshot payload')).toBeInTheDocument()
    expect(fetchMessageToolCalls).toHaveBeenCalledWith(42, expect.any(AbortSignal))

    rerender(<MessageBubble message={{ ...message }} />)
    expect(screen.getByText('full snapshot payload')).toBeInTheDocument()
  })

  it('does not show a reasoning summary', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-thinking',
          role: 'assistant',
          content: 'Done.',
          thinking: 'Checked the available context.',
        }}
      />,
    )

    expect(screen.getByText('Done.')).toBeInTheDocument()
    expect(screen.queryByText('Reasoning summary')).not.toBeInTheDocument()
    expect(screen.queryByText('Checked the available context.')).not.toBeInTheDocument()
  })

  it('keeps active plan progress visible after answer playback starts', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-answer-plan',
          role: 'assistant',
          content: 'The answer is now typing.',
          streaming: true,
          plan: [{
            step_id: 'step-1',
            description: 'Prepare response',
            tool_hints: [],
            depends_on: [],
            success_criteria: '',
            status: 'active',
          }],
          stepProgress: [{ step_id: 'step-1', description: 'Prepare response', status: 'active' }],
        }}
      />,
    )

    expect(screen.getByText('The answer is now typing.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prepare response' })).toBeInTheDocument()
  })

  it('shows progress before a simple answer without repeating its text', () => {
    const answer = 'I am Monaw, your local assistant.'
    const { rerender } = render(
      <MessageBubble
        message={{
          id: 'msg-answer-progress',
          role: 'assistant',
          content: '',
          streaming: true,
          activityItems: [
            { id: 'progress-final', type: 'progress', content: answer },
          ],
        }}
      />,
    )

    const article = screen.getByText(answer).closest('article')
    expect(article).toBeTruthy()
    expect(article).not.toHaveClass('animate-slide-up')
    expect(screen.getByText(answer)).toBeInTheDocument()

    rerender(
      <MessageBubble
        message={{
          id: 'msg-answer-progress',
          role: 'assistant',
          content: answer,
          streaming: true,
          activityItems: [
            { id: 'progress-final', type: 'progress', content: answer },
          ],
        }}
      />,
    )

    expect(screen.getAllByText(answer)).toHaveLength(1)
    expect(screen.getByText('Working')).toBeInTheDocument()
    expect(screen.getByText(answer).closest('article')).toBe(article)

    rerender(
      <MessageBubble
        message={{
          id: 'msg-answer-progress',
          role: 'assistant',
          content: answer,
          streaming: false,
          responseDurationMs: 1200,
          activityItems: [
            { id: 'progress-final', type: 'progress', content: answer },
          ],
        }}
      />,
    )

    expect(screen.getAllByText(answer)).toHaveLength(1)
    expect(screen.getByText('Worked for 1.2s')).toBeInTheDocument()
    expect(screen.getByText(answer).closest('article')).toBe(article)
  })

  it('replaces greeting progress when the final answer starts with it', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-greeting',
          role: 'assistant',
          content: 'Hi Kai! What can I help you with?',
          activityItems: [
            { id: 'progress-greeting', type: 'progress', content: 'Hi Kai!' },
          ],
        }}
      />,
    )

    expect(screen.getAllByText(/Hi Kai!/)).toHaveLength(1)
    expect(screen.getByText('Hi Kai! What can I help you with?')).toBeInTheDocument()
  })

  it('keeps a streamed reply mounted when it completes', () => {
    const { rerender } = render(
      <MessageBubble
        message={{
          id: 'msg-completion-transition',
          role: 'assistant',
          content: 'The answer is arriving.',
          streaming: true,
          responseStartedAtMs: 100,
          activityItems: [
            { id: 'progress-1', type: 'progress', content: 'Checking the result.' },
          ],
        }}
      />,
    )
    const article = screen.getByText('Checking the result.').closest('article')

    rerender(
      <MessageBubble
        message={{
          id: 'msg-completion-transition',
          role: 'assistant',
          content: 'The answer is arriving.',
          streaming: false,
          runStatus: 'complete',
          responseStartedAtMs: 100,
          responseDurationMs: 1200,
          activityItems: [
            { id: 'progress-1', type: 'progress', content: 'Checking the result.' },
          ],
        }}
      />,
    )

    expect(screen.getByText('Checking the result.').closest('article')).toBe(article)
    expect(screen.getByText('Worked for 1.2s')).toBeInTheDocument()
  })

})
