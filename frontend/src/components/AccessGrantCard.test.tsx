import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AccessGrantCard } from './AccessGrantCard'
import { resolveAccessGrant } from '../lib/api/accessGrants'
import { ApiError } from '../lib/api/client'

vi.mock('../lib/api/accessGrants', () => ({
  resolveAccessGrant: vi.fn(),
}))

const ticket = {
  ticket_id: 'grant-1',
  target_type: 'path',
  target_identifier: 'C:\\Users\\KL',
  display_name: 'C:\\Users\\KL',
  action_context: 'Read file tree under: C:\\Users\\KL',
  requested_access: 'read',
}

describe('AccessGrantCard', () => {
  afterEach(() => {
    cleanup()
    vi.mocked(resolveAccessGrant).mockReset()
  })

  it('renders inline and resolves the chosen decision', async () => {
    vi.mocked(resolveAccessGrant).mockResolvedValue({} as Awaited<ReturnType<typeof resolveAccessGrant>>)
    const onResolved = vi.fn()
    render(<AccessGrantCard ticket={ticket} onResolved={onResolved} />)

    expect(screen.getByRole('region', { name: 'Access required' })).toHaveTextContent('read · C:\\Users\\KL')
    fireEvent.click(screen.getByRole('button', { name: 'This session' }))

    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1))
    expect(resolveAccessGrant).toHaveBeenCalledWith('grant-1', 'session')
  })

  it('moves on without an error when the ticket is no longer pending', async () => {
    vi.mocked(resolveAccessGrant).mockRejectedValue(
      new ApiError(new Response(null, { status: 404 }), { code: '', message: '', request_id: '', details: {} }, 'gone'),
    )
    const onResolved = vi.fn()
    render(<AccessGrantCard ticket={ticket} onResolved={onResolved} />)

    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))

    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('stays open with an error when the decision is not accepted', async () => {
    vi.mocked(resolveAccessGrant).mockRejectedValue(new Error('Failed to resolve access grant'))
    const onResolved = vi.fn()
    render(<AccessGrantCard ticket={ticket} onResolved={onResolved} />)

    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not send your decision')
    expect(onResolved).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(onResolved).toHaveBeenCalledTimes(1)
  })
})
