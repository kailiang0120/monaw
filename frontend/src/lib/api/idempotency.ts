const STORAGE_PREFIX = 'monaw.run-command-idempotency.v1:'
const memoryKeys = new Map<string, string>()

function fingerprint(value: string): string {
  // This only names a local retry slot. The key sent to the API remains random.
  // Two independent 32-bit hashes keep request data out of localStorage keys.
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index)
    first = Math.imul(first ^ code, 0x01000193) >>> 0
    second = Math.imul(second ^ (code + index), 0x85ebca6b) >>> 0
  }
  return `${first.toString(16).padStart(8, '0')}${second.toString(16).padStart(8, '0')}`
}

function newKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `monaw-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

/**
 * Reuse one opaque idempotency key for the same logical request across retries,
 * hook remounts, and desktop restarts. Only the random key is persisted; request
 * bodies (which can contain file paths or contents) stay out of browser storage.
 */
export function getPersistentIdempotencyKey(scope: unknown): string {
  const slot = `${STORAGE_PREFIX}${fingerprint(JSON.stringify(scope))}`
  const cached = memoryKeys.get(slot)
  if (cached) return cached

  try {
    const persisted = globalThis.localStorage?.getItem(slot)
    if (persisted) {
      memoryKeys.set(slot, persisted)
      return persisted
    }
  } catch {
    // Storage can be unavailable in restricted renderer contexts.
  }

  const key = newKey()
  memoryKeys.set(slot, key)
  try {
    globalThis.localStorage?.setItem(slot, key)
  } catch {
    // The in-memory value still keeps retries in this renderer consistent.
  }
  return key
}

export function resetIdempotencyKeysForTests(): void {
  memoryKeys.clear()
  try {
    const storage = globalThis.localStorage
    if (!storage) return
    const keys: string[] = []
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index)
      if (key?.startsWith(STORAGE_PREFIX)) keys.push(key)
    }
    keys.forEach((key) => storage.removeItem(key))
  } catch {
    // Test cleanup should not fail if storage is blocked.
  }
}
