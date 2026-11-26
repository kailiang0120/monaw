import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { fetchModelOptions } from '../lib/api/settings'
import { refreshModelCatalog, useModelCatalogState } from './useModelCatalog'
import type { ModelOptions } from '../lib/api/types'

vi.mock('../lib/api/settings', () => ({ fetchModelOptions: vi.fn() }))
afterEach(cleanup)

/** Display the shared store through an independent mounted consumer. */
function Consumer({ name }: { name: string }) {
  const { catalog, status } = useModelCatalogState()
  return <output aria-label={name} data-state={status}>{catalog.providers[0]?.label ?? 'Loading'}</output>
}

it('shares one request, ignores stale refreshes, and keeps the last good catalog on failure', async () => {
  let resolveOld!: (value: ModelOptions) => void
  vi.mocked(fetchModelOptions).mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve }))
  render(<><Consumer name="chat" /><Consumer name="settings" /></>)
  expect(fetchModelOptions).toHaveBeenCalledTimes(1)
  expect(screen.getByLabelText('chat')).toHaveAttribute('data-state', 'loading')
  const fresh: ModelOptions = { providers: [{ id: 'openai', label: 'Updated connection', models: [] }] }
  vi.mocked(fetchModelOptions).mockResolvedValueOnce(fresh)
  await act(async () => { window.dispatchEvent(new Event('monaw:model-catalog-changed')) })
  await waitFor(() => expect(screen.getByLabelText('chat')).toHaveTextContent('Updated connection'))
  expect(screen.getByLabelText('settings')).toHaveTextContent('Updated connection')
  expect(screen.getByLabelText('chat')).toHaveAttribute('data-state', 'ready')
  await act(async () => { resolveOld({ providers: [] }) })
  expect(screen.getByLabelText('chat')).toHaveTextContent('Updated connection')
  vi.mocked(fetchModelOptions).mockRejectedValueOnce(new Error('offline'))
  await act(async () => { await refreshModelCatalog(true) })
  expect(screen.getByLabelText('settings')).toHaveTextContent('Updated connection')
  expect(screen.getByLabelText('chat')).toHaveAttribute('data-state', 'error')
})
