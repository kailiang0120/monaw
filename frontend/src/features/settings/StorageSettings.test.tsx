import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StorageSettings } from './StorageSettings'
import { cleanStorage, fetchStorageUsage } from '../../lib/api/settings'
import type { StorageCategory, StorageUsage } from '../../lib/api/types'

vi.mock('../../lib/api/settings', () => ({ fetchStorageUsage: vi.fn(), cleanStorage: vi.fn() }))

const MB = 1024 * 1024
const category = (overrides: Partial<StorageCategory>): StorageCategory => ({
  id: 'other', label: 'Other', description: '', bytes: 0, cleanable_bytes: 0, clean: null, clean_label: '', warning: '', ...overrides,
})
const usage = (activityBytes = 120 * MB, cacheBytes = 30 * MB): StorageUsage => ({
  location: 'C:\\Users\\me\\.monaw',
  total_bytes: 2 * MB + activityBytes + cacheBytes + 100 * MB,
  categories: [
    category({ id: 'chats', label: 'Chat history', bytes: 2 * MB, cleanable_bytes: 2 * MB, clean: 'backend', clean_label: 'Delete all chats', warning: 'Every conversation is deleted.' }),
    category({ id: 'activity', label: 'Activity log', bytes: activityBytes, cleanable_bytes: activityBytes, clean: 'backend', clean_label: 'Clear activity log' }),
    category({ id: 'app_cache', label: 'App cache', bytes: cacheBytes, cleanable_bytes: cacheBytes, clean: 'app', clean_label: 'Clear app cache' }),
    category({ id: 'account', label: 'ChatGPT sign-in', bytes: 100 * MB }),
    category({ id: 'workspace', label: 'Workspace', bytes: 0 }),
  ],
})

describe('StorageSettings', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(fetchStorageUsage).mockResolvedValue(usage())
  })
  afterEach(() => {
    cleanup()
    delete (window as any).electronAPI
    vi.restoreAllMocks()
  })

  it('shows the total, what can be cleaned and each category', async () => {
    render(<StorageSettings />)

    expect(await screen.findByText('252.0 MB')).toBeInTheDocument()
    expect(screen.getByText('152.0 MB can be cleaned')).toBeInTheDocument()
    expect(screen.getByText('ChatGPT sign-in')).toBeInTheDocument()
    expect(screen.queryByText('Workspace')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Clear|Delete/ })).toHaveLength(3)
  })

  it('cleans a category and reports the space freed', async () => {
    vi.mocked(cleanStorage).mockResolvedValue({ freed_bytes: 120 * MB, usage: usage(0) })
    render(<StorageSettings />)

    fireEvent.click(await screen.findByRole('button', { name: 'Clear activity log' }))

    expect(await screen.findByText('Freed 120.0 MB from activity log.')).toBeInTheDocument()
    expect(cleanStorage).toHaveBeenCalledWith('activity')
    expect(screen.getByRole('button', { name: 'Clear activity log' })).toBeDisabled()
  })

  it('asks before deleting chats and does nothing when declined', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<StorageSettings />)

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all chats' }))

    expect(confirm).toHaveBeenCalledWith('Delete all chats? Every conversation is deleted.')
    expect(cleanStorage).not.toHaveBeenCalled()
  })

  it('clears the app cache through the desktop app', async () => {
    const clearAppCache = vi.fn().mockResolvedValue(true)
    ;(window as any).electronAPI = { clearAppCache }
    render(<StorageSettings />)
    vi.mocked(fetchStorageUsage).mockResolvedValue(usage(120 * MB, 2 * MB))

    fireEvent.click(await screen.findByRole('button', { name: 'Clear app cache' }))

    expect(await screen.findByText('Freed 28.0 MB from app cache.')).toBeInTheDocument()
    expect(clearAppCache).toHaveBeenCalled()
    expect(cleanStorage).not.toHaveBeenCalled()
  })

  it('shows why a cleanup was refused', async () => {
    vi.mocked(cleanStorage).mockRejectedValue(new Error('Wait for the current reply to finish before deleting chats.'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<StorageSettings />)

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all chats' }))

    await waitFor(() => expect(screen.getByText('Wait for the current reply to finish before deleting chats.')).toBeInTheDocument())
  })
})
