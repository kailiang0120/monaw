import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { actionErrorMessage, useItemActions } from './useItemActions'
import { ApiError } from '../lib/api/client'

afterEach(cleanup)

it('keeps independent outcomes and blocks conflicting requests for a pending item', async () => {
  const invalidate = vi.fn()
  const { result } = renderHook(() => useItemActions(invalidate))
  let fail!: (reason: Error) => void
  const first = new Promise<string>((_, reject) => { fail = reject })
  const commit = vi.fn()
  const duplicate = vi.fn()
  let pending!: Promise<string | undefined>
  act(() => { pending = result.current.perform('first', 'Saving', () => first, commit) })
  expect(result.current.feedback.first.pending).toBe('Saving')
  await act(async () => {
    await result.current.perform('first', 'Duplicate', duplicate, commit)
    await result.current.perform('second', 'Saving', async () => 'second saved', commit)
    fail(new Error('First failed'))
    await pending
  })
  expect(duplicate).not.toHaveBeenCalled()
  expect(commit).toHaveBeenCalledExactlyOnceWith('second saved')
  expect(result.current.feedback.second.success).toBe('Saved')
  expect(result.current.feedback.first.error).toBe('First failed')
})

it('does not apply a response after unmount', async () => {
  let finish!: (value: string) => void
  const request = new Promise<string>((resolve) => { finish = resolve })
  const commit = vi.fn()
  const { result, unmount } = renderHook(() => useItemActions(vi.fn()))
  let pending!: Promise<string | undefined>
  act(() => { pending = result.current.perform('row', 'Saving', () => request, commit) })
  unmount()
  finish('late')
  await pending
  expect(commit).not.toHaveBeenCalled()
})

it('bounds feedback and removes common credential forms while retaining a request reference', () => {
  const error = new ApiError(new Response('{}', { status: 500 }), {
    message: 'Failed: Bearer sensitive token=secret-value https://name:pass@example.test',
    request_id: 'req-2', code: 'failed', details: {},
  }, 'Failed')
  const message = actionErrorMessage(error, 'Failed')
  expect(message).not.toContain('sensitive')
  expect(message).not.toContain('secret-value')
  expect(message).not.toContain('name:pass')
  expect(message).toContain('req-2')
})
