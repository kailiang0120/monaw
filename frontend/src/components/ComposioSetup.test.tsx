import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ComposioSetup } from './ComposioSetup'
import { ComposioAccountPanel } from './ComposioAccountPanel'
import { connectorRequest } from '../lib/api/connectors'

vi.mock('../lib/api/connectors', () => ({ connectorRequest: vi.fn() }))

afterEach(() => {
  cleanup()
  delete (window as any).electronAPI
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.useRealTimers()
})

it('guides setup and hands keys only to privileged desktop IPC, then removes them', async () => {
  vi.mocked(connectorRequest).mockResolvedValue({ configured: false })
  const changeComposioKey = vi.fn().mockResolvedValue({ ok: true })
  ;(window as any).electronAPI = { changeComposioKey }
  const onChanged = vi.fn()
  render(<ComposioSetup onChanged={onChanged} />)
  await waitFor(() => expect(onChanged).toHaveBeenCalledWith(false))
  expect(screen.getByRole('link', { name: 'Open Composio' })).toHaveAttribute(
    'href',
    'https://dashboard.composio.dev/',
  )
  expect(screen.getByText('Setup guide')).toBeInTheDocument()
  const input = screen.getByLabelText('Platform API key')
  expect(input).toHaveAttribute('type', 'password')
  fireEvent.change(input, { target: { value: 'private-test-key' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save key' }))
  await screen.findByText('Configured')
  expect(changeComposioKey).toHaveBeenCalledWith({ action: 'save', value: 'private-test-key' })
  expect(screen.queryByDisplayValue('private-test-key')).not.toBeInTheDocument()
  expect(connectorRequest).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Remove key' }))
  await waitFor(() => expect(changeComposioKey).toHaveBeenLastCalledWith({ action: 'remove' }))
  await screen.findByLabelText('Platform API key')
  expect(onChanged).toHaveBeenLastCalledWith(false)
})

it('opens hosted sign-in, completes polling, and rejects a foreign sign-in origin', async () => {
  vi.useFakeTimers()
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  const changed = vi.fn()
  let url = 'https://connect.composio.dev/link/example'
  vi.mocked(connectorRequest).mockImplementation(async (path) => {
    if (path.endsWith('/poll')) return { status: 'connected' }
    return { login_id: 'test', authorization_url: url, interval: 3, expires_in: 600 }
  })
  const view = render(
    <ComposioAccountPanel providerId="gmail" configured accounts={[]} onChanged={changed} />,
  )
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' })))
  expect(open).toHaveBeenCalledWith(url, '_blank', 'noopener,noreferrer')
  expect(screen.getByRole('link', { name: 'Continue to Gmail' })).toHaveAttribute('href', url)
  await act(async () => vi.advanceTimersByTimeAsync(3000))
  expect(changed).toHaveBeenCalledOnce()
  expect(screen.getByText('Gmail connected.')).toBeInTheDocument()
  url = 'https://connect.composio.dev.attacker.test/link/example'
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' })))
  expect(open).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('alert')).toHaveTextContent('sign-in address was invalid')
  expect(connectorRequest).toHaveBeenCalledWith('/composio/login/test', 'DELETE')
  view.unmount()
})

it('cancels an outstanding attempt when its panel closes', async () => {
  vi.spyOn(window, 'open').mockReturnValue(null)
  vi.mocked(connectorRequest).mockResolvedValue({
    login_id: 'pending',
    authorization_url: 'https://connect.composio.dev/link/pending',
    interval: 3,
    expires_in: 600,
  })
  const view = render(
    <ComposioAccountPanel providerId="outlook" configured accounts={[]} onChanged={vi.fn()} />,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Connect Outlook' }))
  await screen.findByRole('link', { name: 'Continue to Outlook' })
  view.unmount()
  expect(connectorRequest).toHaveBeenCalledWith('/composio/login/pending', 'DELETE')
})
