import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ExecutionEnvironment } from '../lib/api/types'
import { ApprovalToast } from './ApprovalToast'
import { MessageBubble } from './MessageBubble'

const { pending } = vi.hoisted(() => ({ pending: vi.fn() }))
vi.mock('../lib/api/approvals', () => ({
  fetchPendingApprovals: pending, approveTicket: vi.fn(), rejectTicket: vi.fn(),
}))

const environment: ExecutionEnvironment = {
  backend: 'local_direct', summary: 'Runs on your PC — no filesystem or network isolation',
  workspace: 'C:/work', execution_workspace: 'C:/work', shell: 'powershell',
  filesystem: 'Host filesystem accessible', network: 'Configured network deny is not enforced',
  writes: 'Writes affect host files directly', approval_required: true,
  reason: 'Docker is unavailable', scope: 'Shell command only; personal Chrome uses its own permissions.',
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

it('shows the host boundary beside the streaming approval action', () => {
  render(<MessageBubble message={{ id: '1', role: 'assistant', content: '', approvals: [{
    type: 'required', ticket_id: 'ticket', action: 'Run command', reason: '', execution_environment: environment,
  }] }} />)
  expect(screen.getByText(environment.summary)).toBeInTheDocument()
  expect(screen.getByText(environment.network)).toBeInTheDocument()
  expect(screen.getByText('Workspace: C:/work')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled()
})

it('retains the environment in resumed approval progress', () => {
  render(<MessageBubble message={{ id: '2', role: 'assistant', content: '', approvals: [{
    type: 'resumed', ticket_id: 'ticket', action: 'Run command', reason: '', execution_environment: environment,
  }] }} />)
  expect(screen.getByText('Executing')).toBeInTheDocument()
  expect(screen.getByText(environment.summary)).toBeInTheDocument()
})

it('shows the same environment in a polled approval toast', async () => {
  pending.mockResolvedValue([{ id: 'ticket', action_description: 'Run command', reason: '',
    payload: { args: { sandbox: { execution_environment: environment } } } }])
  render(<ApprovalToast conversationId="chat" isStreaming={false} />)
  expect(await screen.findByText(environment.summary)).toBeInTheDocument()
  expect(screen.getByText(environment.writes)).toBeInTheDocument()
})

it('shows effective execution details when the tool result is opened', () => {
  render(<MessageBubble message={{ id: '3', role: 'assistant', content: '', toolCalls: [{
    tool: 'exec', input: '{"command":"echo hi"}', output: JSON.stringify({
      status: 'ok', sandbox: { execution_environment: environment },
    }),
  }] }} />)
  fireEvent.click(screen.getByRole('button', { expanded: false }))
  fireEvent.click(screen.getByRole('button', { expanded: false }))
  expect(screen.getByText(environment.summary)).toBeInTheDocument()
})

it('keeps malformed tool output available without rendering an invalid disclosure', () => {
  render(<MessageBubble message={{ id: '4', role: 'assistant', content: '', toolCalls: [{
    tool: 'exec', input: '{}', output: JSON.stringify({ sandbox: { execution_environment: { summary: {} } } }),
  }] }} />)
  fireEvent.click(screen.getByRole('button', { expanded: false }))
  fireEvent.click(screen.getByRole('button', { expanded: false }))
  expect(screen.queryByLabelText('Execution environment')).not.toBeInTheDocument()
  expect(screen.getByText('Result')).toBeInTheDocument()
})
