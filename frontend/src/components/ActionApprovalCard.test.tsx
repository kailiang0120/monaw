import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ActionDescriptor } from '../lib/api/types'
import { ActionApprovalCard } from './ActionApprovalCard'
import { fetchApprovalPreview } from '../lib/api/approvals'

vi.mock('../lib/api/approvals', () => ({ fetchApprovalPreview: vi.fn() }))

const base: ActionDescriptor = {
  action_id: 'action-1',
  tool_name: 'click',
  action_kind: 'submit',
  destination: 'https://example.com',
  side_effect: 'external_mutation',
  target: { page_id: 1 },
}

describe('ActionApprovalCard', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('does not promise a destination limit for actions Monaw cannot inspect', () => {
    render(
      <ActionApprovalCard
        action={{ ...base, action_kind: 'execute_code', side_effect: 'unknown', opaque: true }}
      />,
    )
    expect(screen.getByText(/does not limit what it sends or where/)).toBeInTheDocument()
    expect(
      screen.queryByText(/applies once to this action and destination/),
    ).not.toBeInTheDocument()
  })

  it('warns that page scripts can still change a browser submission', () => {
    render(<ActionApprovalCard action={base} />)
    expect(screen.getByText(/applies once to this action and destination/)).toHaveTextContent(
      'Scripts on the page can still change what is sent after you approve.',
    )
  })

  it('lists other destinations such as link pings', () => {
    render(
      <ActionApprovalCard action={{ ...base, also_sends_to: ['https://collector.example.net'] }} />,
    )
    expect(screen.getByText('Also sends to')).toBeInTheDocument()
    expect(screen.getByText('https://collector.example.net')).toBeInTheDocument()
  })
})

it('loads exact action details privately and keeps approval unavailable after a lost or changed preview', async () => {
  const onPreviewReady = vi.fn()
  vi.mocked(fetchApprovalPreview).mockResolvedValue({
    authorization_binding: 'binding-1',
    parameters: {
      command: 'python scripts/migrate.py',
      script: '<script>untrusted()</script>',
      env: { API_KEY: '[REDACTED]' },
    },
  })
  const action = { ...base, preview_required: true, authorization_binding: 'binding-1' }
  const view = render(
    <ActionApprovalCard action={action} ticketId="ticket-1" onPreviewReady={onPreviewReady} />,
  )
  expect(screen.getByRole('status')).toHaveTextContent('Loading action details')
  expect(await screen.findByText('python scripts/migrate.py')).toBeVisible()
  expect(screen.getByText('<script>untrusted()</script>')).toBeVisible()
  expect(view.container.querySelector('script')).toBeNull()
  expect(onPreviewReady).toHaveBeenLastCalledWith(true)
  vi.mocked(fetchApprovalPreview).mockResolvedValue({
    authorization_binding: 'changed-binding',
    parameters: {},
  })
  view.rerender(
    <ActionApprovalCard action={action} ticketId="ticket-2" onPreviewReady={onPreviewReady} />,
  )
  expect(await screen.findByRole('alert')).toHaveTextContent('Reject this request')
  expect(onPreviewReady).toHaveBeenLastCalledWith(false)
  expect(screen.queryByText('python scripts/migrate.py')).not.toBeInTheDocument()
  view.rerender(<ActionApprovalCard action={action} />)
  await waitFor(() =>
    expect(screen.queryByRole('region', { name: 'Action details' })).not.toBeInTheDocument(),
  )
  expect(fetchApprovalPreview).toHaveBeenCalledTimes(2)
})
