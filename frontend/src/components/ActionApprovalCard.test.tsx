import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { ActionDescriptor } from '../lib/api/types'
import { ActionApprovalCard } from './ActionApprovalCard'

const base: ActionDescriptor = {
  action_id: 'action-1', tool_name: 'click', action_kind: 'submit',
  destination: 'https://example.com', side_effect: 'external_mutation', target: { page_id: 1 },
}

describe('ActionApprovalCard', () => {
  afterEach(cleanup)

  it('does not promise a destination limit for actions Monaw cannot inspect', () => {
    render(<ActionApprovalCard action={{ ...base, action_kind: 'execute_code', side_effect: 'unknown', opaque: true }} />)
    expect(screen.getByText(/does not limit what it sends or where/)).toBeInTheDocument()
    expect(screen.queryByText(/applies once to this action and destination/)).not.toBeInTheDocument()
  })

  it('warns that page scripts can still change a browser submission', () => {
    render(<ActionApprovalCard action={base} />)
    expect(screen.getByText(/applies once to this action and destination/)).toHaveTextContent(
      'Scripts on the page can still change what is sent after you approve.')
  })

  it('lists other destinations such as link pings', () => {
    render(<ActionApprovalCard action={{ ...base, also_sends_to: ['https://collector.example.net'] }} />)
    expect(screen.getByText('Also sends to')).toBeInTheDocument()
    expect(screen.getByText('https://collector.example.net')).toBeInTheDocument()
  })
})
