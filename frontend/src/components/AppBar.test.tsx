import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AppBar } from './AppBar'

const props = {
  sidebarCollapsed: false,
  onToggleSidebar: () => {},
  onNewChat: () => {},
  status: 'ready' as const,
  approvalMode: 'default' as const,
}

function installElectron(platform: string) {
  let notify: (maximized: boolean) => void = () => {}
  const api = {
    platform,
    windowControl: vi.fn().mockResolvedValue(false),
    isWindowMaximized: vi.fn().mockResolvedValue(false),
    onWindowMaximized: vi.fn((listener: (maximized: boolean) => void) => {
      notify = listener
      return () => { notify = () => {} }
    }),
  }
  ;(window as any).electronAPI = api
  return { api, notify: (maximized: boolean) => notify(maximized) }
}

describe('AppBar window buttons', () => {
  afterEach(() => {
    cleanup()
    delete (window as any).electronAPI
  })

  it('minimizes, maximizes and closes the window on Windows', async () => {
    const { api } = installElectron('win32')
    render(<AppBar {...props} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Minimize' }))
    fireEvent.click(screen.getByRole('button', { name: 'Maximize' }))
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    expect(api.windowControl.mock.calls).toEqual([['minimize'], ['toggle-maximize'], ['close']])
  })

  it('offers Restore while the window is maximized', async () => {
    const window = installElectron('win32')
    const { unmount } = render(<AppBar {...props} />)

    await act(async () => window.notify(true))
    expect(screen.getByRole('button', { name: 'Restore' })).toBeInTheDocument()

    await act(async () => window.notify(false))
    expect(screen.getByRole('button', { name: 'Maximize' })).toBeInTheDocument()
    unmount()
  })

  it('leaves macOS with its own traffic lights', () => {
    installElectron('darwin')
    render(<AppBar {...props} />)

    expect(screen.queryByRole('group', { name: 'Window' })).not.toBeInTheDocument()
  })
})
