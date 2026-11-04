import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { TurnChanges } from './TurnChanges'
import type { ToolCall } from '../hooks/useChat'

const write = (path: string): ToolCall => ({
  tool: 'fs_write', input: JSON.stringify({ path, content: '' }), output: '{"status":"ok"}', status: 'ok',
})

describe('TurnChanges', () => {
  afterEach(cleanup)

  it('renders nothing for a reply that changed nothing', () => {
    const { container } = render(<TurnChanges toolCalls={[{ tool: 'fs_read', input: '{"path":"a"}', status: 'ok' }]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('lists the files a reply wrote and expands long lists', () => {
    render(<TurnChanges toolCalls={Array.from({ length: 8 }, (_, index) => write(`src/file-${index}.ts`))} />)
    expect(screen.getByLabelText('Changes in this reply')).toHaveTextContent('8 files changed')
    expect(screen.getByText('file-0.ts')).toBeInTheDocument()
    expect(screen.queryByText('file-7.ts')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Show 2 more/ }))
    expect(screen.getByText('file-7.ts')).toBeInTheDocument()
  })

  it('says shell commands ran when it cannot list their files', () => {
    render(<TurnChanges toolCalls={[{ tool: 'exec', input: '{"command":"npx create-next-app ."}', status: 'ok', output: 'done' }]} />)
    expect(screen.getByLabelText('Changes in this reply')).toHaveTextContent('Ran 1 command; files changed by commands are not listed.')
  })
})
