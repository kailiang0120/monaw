import { afterEach, describe, expect, it, vi } from 'vitest'

import { getPersistentIdempotencyKey, resetIdempotencyKeysForTests } from './idempotency'

describe('persistent request idempotency keys', () => {
  afterEach(() => {
    resetIdempotencyKeysForTests()
  })

  it('reuses keys for an identical logical request and isolates different scopes', async () => {
    const scope = ['file-mutation', 'apply', 'mutation-private-1', { expected_revision: 8 }]
    const sameRequest = ['file-mutation', 'apply', 'mutation-private-1', { expected_revision: 8 }]
    const differentRequest = ['file-mutation', 'apply', 'mutation-private-2', { expected_revision: 8 }]

    const key = getPersistentIdempotencyKey(scope)

    expect(getPersistentIdempotencyKey(sameRequest)).toBe(key)
    expect(getPersistentIdempotencyKey(differentRequest)).not.toBe(key)

    const stored = Array.from({ length: localStorage.length }, (_, index) => {
      const storageKey = localStorage.key(index) ?? ''
      return [storageKey, localStorage.getItem(storageKey) ?? '']
    })
    expect(stored.length).toBeGreaterThanOrEqual(2)
    expect(JSON.stringify(stored)).not.toContain('mutation-private-1')
    expect(JSON.stringify(stored)).not.toContain('mutation-private-2')

    vi.resetModules()
    const reloaded = await import('./idempotency')
    expect(reloaded.getPersistentIdempotencyKey(scope)).toBe(key)
    reloaded.resetIdempotencyKeysForTests()
  })

  it('keeps file contents and paths out of both storage slots and stored values', () => {
    const sensitivePath = 'C:/private/customer-report.txt'
    const sensitiveContent = 'account number 123-45-6789\nsecret draft text'
    const scope = ['file-mutation', 'preview', { path: sensitivePath, content: sensitiveContent }]

    const key = getPersistentIdempotencyKey(scope)
    const stored = Array.from({ length: localStorage.length }, (_, index) => {
      const storageKey = localStorage.key(index) ?? ''
      return [storageKey, localStorage.getItem(storageKey) ?? '']
    })

    expect(key).not.toContain(sensitivePath)
    expect(key).not.toContain(sensitiveContent)
    expect(JSON.stringify(stored)).not.toContain(sensitivePath)
    expect(JSON.stringify(stored)).not.toContain(sensitiveContent)
  })
})
