import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AboutSettings } from './AboutSettings'

afterEach(() => { cleanup(); delete window.electronAPI })

test('shows the installed version and lets the user check for updates', async () => {
  const check = vi.fn().mockResolvedValue({ currentVersion: '1.1.0', status: 'up-to-date' })
  Object.defineProperty(window, 'electronAPI', { configurable: true, value: {
    updateStatus: vi.fn().mockResolvedValue({ currentVersion: '1.1.0', status: 'idle' }),
    checkForUpdates: check,
    onUpdateState: vi.fn().mockReturnValue(vi.fn()),
  } })
  render(<AboutSettings />)
  expect(await screen.findByText('1.1.0')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Check for updates' }))
  expect(await screen.findByText('You are up to date.')).toBeTruthy()
  expect(check).toHaveBeenCalledOnce()
})

test('requires a confirmation before restarting to install', async () => {
  const install = vi.fn().mockResolvedValue(true)
  Object.defineProperty(window, 'electronAPI', { configurable: true, value: {
    updateStatus: vi.fn().mockResolvedValue({ currentVersion: '1.1.0', status: 'ready', latestVersion: '1.2.0' }),
    installUpdate: install,
    onUpdateState: vi.fn().mockReturnValue(vi.fn()),
  } })
  render(<AboutSettings />)
  fireEvent.click(await screen.findByRole('button', { name: 'Restart to update' }))
  expect(install).not.toHaveBeenCalled()
  expect(screen.getByText(/Finish or pause active work/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Restart and install' }))
  await waitFor(() => expect(install).toHaveBeenCalledOnce())
})
