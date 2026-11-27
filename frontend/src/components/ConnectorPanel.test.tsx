import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ConnectorPanel } from './ConnectorPanel'
import { connectorRequest } from '../lib/api/connectors'

vi.mock('../lib/api/connectors', () => ({ connectorRequest: vi.fn() }))

/** Supply metadata and a pending sign-in without exposing any token to the UI. */
function mockRequests() {
  vi.mocked(connectorRequest).mockImplementation(async (path) => {
    if (path === '/providers')
      return {
        providers: [
          {
            id: 'github',
            name: 'GitHub',
            capabilities: [
              { id: 'read', action: 'read', description: 'Read GitHub repositories' },
              { id: 'create', action: 'create', description: 'Create issues and pull requests' },
            ],
          },
        ],
      }
    if (path === '/accounts') return { accounts: [] }
    if (path === '/github/configuration')
      return {
        device_login_available: true,
        installation_url: 'https://github.com/apps/monaw-agent/installations/new',
      }
    if (path === '/github/login')
      return {
        login_id: 'attempt',
        user_code: 'ABCD-EFGH',
        interval: 5,
        expires_in: 900,
        verification_uri: 'https://github.com/login/device',
      }
    return { cancelled: true }
  })
}

afterEach(() => {
  cleanup()
  delete (window as any).electronAPI
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.clearAllMocks()
})

it('opens connector setup on tap, starts read-only sign-in, and cancels the server attempt on close', async () => {
  mockRequests()
  const view = render(<ConnectorPanel />)
  expect(screen.queryByRole('button', { name: 'Connect GitHub' })).not.toBeInTheDocument()

  fireEvent.click(await screen.findByRole('button', { name: /GitHub/i }))
  fireEvent.click(await screen.findByRole('button', { name: 'Connect GitHub' }))

  expect(await screen.findByText('ABCD-EFGH')).toBeInTheDocument()
  expect(connectorRequest).toHaveBeenCalledWith('/github/login', 'POST', { allow_writes: false })
  expect(screen.getByRole('link', { name: 'Continue on GitHub' })).toHaveAttribute(
    'href',
    'https://github.com/login/device',
  )
  view.unmount()
  expect(connectorRequest).toHaveBeenCalledWith('/github/login/attempt', 'DELETE')
})

it('cancels a pending login and returns to the connector grid when All connectors is clicked', async () => {
  mockRequests()
  render(<ConnectorPanel />)

  fireEvent.click(await screen.findByRole('button', { name: /GitHub/i }))
  fireEvent.click(await screen.findByRole('button', { name: 'Connect GitHub' }))
  expect(await screen.findByText('ABCD-EFGH')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'All connectors' }))
  expect(connectorRequest).toHaveBeenCalledWith('/github/login/attempt', 'DELETE')
  expect(await screen.findByRole('button', { name: /GitHub/i })).toBeInTheDocument()
  expect(screen.queryByText('ABCD-EFGH')).not.toBeInTheDocument()
})

it.each(['desktop', 'browser', 'failure'] as const)(
  'copies the GitHub sign-in code with %s clipboard behavior',
  async (mode) => {
    mockRequests()
    const browserWrite = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: browserWrite } })
    const desktopWrite = vi.fn().mockResolvedValue(undefined)

    if (mode !== 'browser') {
      browserWrite.mockRejectedValue(new Error('Clipboard permission denied'))
      Object.defineProperty(window, 'electronAPI', {
        configurable: true,
        value: { writeClipboardText: desktopWrite },
      })
    }
    if (mode === 'failure') {
      desktopWrite.mockRejectedValue(new Error('Fixture OS clipboard failure'))
    }

    render(<ConnectorPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /GitHub/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Connect GitHub' }))
    expect(await screen.findByText('ABCD-EFGH')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))

    if (mode === 'failure') {
      expect(await screen.findByRole('alert')).toHaveTextContent('Select the code and press Ctrl+C')
      expect(screen.queryByRole('button', { name: 'Copied' })).not.toBeInTheDocument()
    } else {
      expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
      expect(mode === 'desktop' ? desktopWrite : browserWrite).toHaveBeenCalledWith('ABCD-EFGH')
    }
    if (mode !== 'browser') expect(browserWrite).not.toHaveBeenCalled()
  },
)

it('waits for the polling interval and refreshes metadata after success', async () => {
  mockRequests()
  render(<ConnectorPanel />)
  fireEvent.click(await screen.findByRole('button', { name: /GitHub/i }))
  await screen.findByRole('button', { name: 'Connect GitHub' })

  vi.useFakeTimers()
  fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub' }))
  await act(async () => {
    await Promise.resolve()
  })
  vi.mocked(connectorRequest).mockResolvedValueOnce({ status: 'connected' })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4999)
  })
  expect(connectorRequest).not.toHaveBeenCalledWith('/github/login/attempt/poll', 'POST')
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1)
  })
  expect(connectorRequest).toHaveBeenCalledWith('/github/login/attempt/poll', 'POST')
  vi.useRealTimers()
  await waitFor(() => expect(screen.getByText('GitHub connected.')).toBeInTheDocument())
  expect(screen.queryByText('ABCD-EFGH')).not.toBeInTheDocument()
})

it('shows Composio setup on the connector grid when mail providers exist and keeps Outlook setup focused when configured', async () => {
  vi.mocked(connectorRequest).mockImplementation(async (path) => {
    if (path === '/providers')
      return {
        providers: [
          {
            id: 'github',
            name: 'GitHub',
            capabilities: [{ id: 'read', action: 'read', description: 'Read' }],
          },
          {
            id: 'gmail',
            name: 'Gmail',
            capabilities: [{ id: 'read', action: 'read', description: 'Read' }],
          },
          {
            id: 'outlook',
            name: 'Outlook',
            capabilities: [{ id: 'read', action: 'read', description: 'Read' }],
          },
        ],
      }
    if (path === '/accounts') return { accounts: [] }
    if (path === '/github/configuration')
      return { device_login_available: true, installation_url: '' }
    if (path === '/composio/configuration') return { configured: true }
    return {}
  })

  render(<ConnectorPanel />)
  expect(await screen.findByText('Composio')).toBeInTheDocument()
  expect(screen.getByText('Configured')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: /Outlook/i }))
  expect(await screen.findByRole('button', { name: 'All connectors' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Connect Outlook' })).toBeEnabled()
  expect(screen.queryByRole('link', { name: 'Open Composio' })).not.toBeInTheDocument()
})

it('groups and filters the catalog, preserves filters on return, and starts a new provider sign-in', async () => {
  vi.mocked(connectorRequest).mockImplementation(async (path) => {
    if (path === '/providers')
      return {
        providers: [
          {
            id: 'slack',
            name: 'Slack',
            category: 'messaging',
            connection_method: 'composio',
            description: 'Search messages.',
            capabilities: [],
          },
          {
            id: 'googledrive',
            name: 'Google Drive',
            category: 'files',
            connection_method: 'composio',
            description: 'Read files and documents.',
            capabilities: [],
          },
          { id: 'github', name: 'GitHub', category: 'development', capabilities: [] },
        ],
      }
    if (path === '/accounts') return { accounts: [] }
    if (path === '/composio/configuration') return { configured: true }
    if (path === '/composio/login')
      return {
        login_id: 'slack-attempt',
        authorization_url: 'https://connect.composio.dev/link/slack',
        interval: 10,
        expires_in: 600,
      }
    return {}
  })
  vi.spyOn(window, 'open').mockImplementation(() => null)
  render(<ConnectorPanel />)
  expect(await screen.findByRole('region', { name: 'Files & documents' })).toBeInTheDocument()
  const messaging = screen.getByRole('region', { name: 'Messaging' })
  const icon = messaging.querySelector('img')!
  expect(icon).toHaveAttribute('alt', '')
  expect(icon.getAttribute('src')).not.toMatch(/^https?:/)
  fireEvent.error(icon)
  expect(messaging.querySelector('img')).toBeNull()
  expect(within(messaging).getByRole('button', { name: /Slack/ })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Connector category' }))
  fireEvent.click(screen.getByRole('option', { name: 'Messaging' }))
  expect(screen.queryByRole('region', { name: 'Files & documents' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Slack/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Connect Slack' }))
  expect(await screen.findByRole('link', { name: 'Continue to Slack' })).toBeInTheDocument()
  expect(connectorRequest).toHaveBeenCalledWith('/composio/login', 'POST', { provider_id: 'slack' })
  fireEvent.click(screen.getByRole('button', { name: 'All connectors' }))
  expect(screen.getByRole('button', { name: 'Connector category' })).toHaveTextContent('Messaging')
  expect(connectorRequest).toHaveBeenCalledWith('/composio/login/slack-attempt', 'DELETE')
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search connectors' }), {
    target: { value: 'missing' },
  })
  expect(screen.getByRole('status')).toHaveTextContent('No connectors match your search.')
})

it('keeps unavailable Google connectors visible and explains why Connect is disabled', async () => {
  vi.mocked(connectorRequest).mockImplementation(async (path) => {
    if (path === '/providers')
      return {
        providers: [
          {
            id: 'gmail',
            name: 'Gmail',
            category: 'email',
            connection_method: 'composio',
            setup_notice: 'Google authorization is unavailable.',
            capabilities: [],
          },
        ],
      }
    if (path === '/accounts') return { accounts: [] }
    if (path === '/composio/configuration') return { configured: true }
    return {}
  })
  render(<ConnectorPanel />)
  const card = await screen.findByRole('button', { name: /Gmail/ })
  expect(card).toHaveClass('st-connector-card-unavailable')
  expect(card).toHaveTextContent('Unavailable')
  fireEvent.click(card)
  expect(await screen.findByRole('button', { name: 'Connect Gmail' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'About Sign-in unavailable' }))
  expect(screen.getByText('Google authorization is unavailable.')).toBeVisible()
})
