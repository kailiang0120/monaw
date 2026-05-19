import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MessageBubble } from './MessageBubble'

describe('MessageBubble', () => {
  afterEach(() => {
    cleanup()
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
    expect(screen.getByText('Monaw')).toBeInTheDocument()
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

    expect(screen.getByText('Hermes')).toBeInTheDocument()
  })

  it('shows response time below completed assistant responses', () => {
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

    const summary = screen.getByText(/Tools executed 1/)
    const timer = screen.getByLabelText('Response time 1.5s')
    expect(timer).toBeInTheDocument()
    expect(summary.compareDocumentPosition(timer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows only answer content while assistant content is streaming', () => {
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
    expect(screen.queryByLabelText('Elapsed time 1.5s')).not.toBeInTheDocument()
    expect(screen.queryByText('Writing response')).not.toBeInTheDocument()
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

  it('renders assistant response attachments as downloadable files and image previews', () => {
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

    expect(screen.getByRole('img', { name: 'chart.png' })).toHaveAttribute(
      'src',
      expect.stringContaining('/api/files/image-1/preview'),
    )
    expect(screen.getByRole('link', { name: /report.pdf/i })).toHaveAttribute('download', 'report.pdf')
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

  it('compresses live progress and tool calls into a scrollable tool panel', () => {
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

    expect(screen.queryByText('Opening the browser.')).not.toBeInTheDocument()
    expect(screen.getByText('Inspecting the page.')).toBeInTheDocument()
    expect(screen.getByRole('tablist', { name: 'Tool calls' })).toBeInTheDocument()
    expect(screen.getAllByRole('tab')).toHaveLength(2)
    expect(screen.getByText('{"include_screenshot":true}')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /browser_tabs/ }))

    expect(screen.getByText('tabs ok')).toBeInTheDocument()
  })

  it('keeps the reasoning trace collapsed behind a Trace toggle until clicked', () => {
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

    expect(screen.getByText('Trace')).toBeInTheDocument()
    expect(screen.queryByText('Checked the available context.')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByText('Checked the available context.')).toBeInTheDocument()
    expect(screen.getByLabelText('Reasoning trace')).toBeInTheDocument()
  })

  it('does not expose reasoning trace while the answer is still streaming', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-live-thinking',
          role: 'assistant',
          content: '',
          streaming: true,
          thinking: 'Still reasoning through the request.',
        }}
      />,
    )

    expect(screen.getByText('Thinking')).toBeInTheDocument()
    expect(screen.getByText('Waiting for the first response token')).toBeInTheDocument()
    expect(screen.queryByText('Trace')).not.toBeInTheDocument()
    expect(screen.queryByText('Still reasoning through the request.')).not.toBeInTheDocument()
  })

  it('hides the live planning card once answer playback has started', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-answer-plan',
          role: 'assistant',
          content: 'The answer is now typing.',
          streaming: true,
          plan: [{ step_id: 'step-1', description: 'Prepare response' }],
          stepProgress: [{ step_id: 'step-1', description: 'Prepare response', status: 'active' }],
        }}
      />,
    )

    expect(screen.getByText('The answer is now typing.')).toBeInTheDocument()
    expect(screen.queryByText('Planning')).not.toBeInTheDocument()
    expect(screen.queryByText('Prepare response')).not.toBeInTheDocument()
  })

  it('hides live progress once answer playback has started', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-answer-progress',
          role: 'assistant',
          content: 'The visible answer.',
          streaming: true,
          activityItems: [
            { id: 'progress-final', type: 'progress', content: 'The leaked progress answer.' },
          ],
        }}
      />,
    )

    expect(screen.getByText('The visible answer.')).toBeInTheDocument()
    expect(screen.queryByText('The leaked progress answer.')).not.toBeInTheDocument()
  })

  it('does not display thinking traces that exceed the visible length limit', () => {
    render(
      <MessageBubble
        message={{
          id: 'msg-long-thinking',
          role: 'assistant',
          content: 'Done.',
          thinking: 'tool signature '.repeat(1_000),
        }}
      />,
    )

    expect(screen.queryByText('Reasoning trace')).not.toBeInTheDocument()
    expect(screen.queryByText(/tool signature/)).not.toBeInTheDocument()
    expect(screen.getByText('Done.')).toBeInTheDocument()
  })
})
