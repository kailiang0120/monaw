const SECRET_FIELDS = Object.freeze({
  openai: { storeKey: 'openai_api_key', backendField: 'openai_api_key' },
  google: { storeKey: 'google_api_key', backendField: 'google_api_key' },
  tavily: { storeKey: 'tavily_api_key', backendField: 'tavily_api_key' },
  telegramBot: { storeKey: 'telegram_bot_token', backendField: 'telegram_bot_token' },
})

const ENCRYPTED_PREFIX = 'safe:v1:'

function assertCredentialId(id) {
  if (typeof id !== 'string' || !SECRET_FIELDS[id]) {
    throw new Error(`Unsupported credential id: ${String(id)}`)
  }
  return id
}

function assertCredentialValue(value) {
  if (typeof value !== 'string') throw new Error('Credential values must be strings')
  if (value.length > 64 * 1024) throw new Error('Credential value is too large')
  return value
}

function createCredentialVault({
  safeStorage,
  store,
  legacyData = {},
  deleteLegacyValue = () => {},
}) {
  function requireEncryption() {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error('OS-backed credential encryption is unavailable')
    }
  }

  function encrypt(value) {
    requireEncryption()
    const encrypted = safeStorage.encryptString(value)
    return `${ENCRYPTED_PREFIX}${Buffer.from(encrypted).toString('base64')}`
  }

  function decrypt(storedValue) {
    requireEncryption()
    if (typeof storedValue !== 'string' || !storedValue.startsWith(ENCRYPTED_PREFIX)) {
      throw new Error('Credential is not stored in the encrypted format')
    }
    return safeStorage.decryptString(
      Buffer.from(storedValue.slice(ENCRYPTED_PREFIX.length), 'base64'),
    )
  }

  function migrate() {
    requireEncryption()
    for (const { storeKey } of Object.values(SECRET_FIELDS)) {
      const currentValue = store.get(storeKey)
      if (typeof currentValue === 'string' && currentValue.startsWith(ENCRYPTED_PREFIX)) {
        deleteLegacyValue(storeKey)
        continue
      }
      const plaintext = typeof currentValue === 'string' && currentValue
        ? currentValue
        : typeof legacyData[storeKey] === 'string'
          ? legacyData[storeKey]
          : ''
      if (!plaintext) continue
      const encrypted = encrypt(plaintext)
      if (decrypt(encrypted) !== plaintext) {
        throw new Error(`Credential migration verification failed for ${storeKey}`)
      }
      store.set(storeKey, encrypted)
      deleteLegacyValue(storeKey)
    }
    for (const key of Object.keys(legacyData)) {
      legacyData[key] = ''
    }
  }

  function status() {
    return Object.fromEntries(
      Object.entries(SECRET_FIELDS).map(([id, { storeKey }]) => {
        const value = store.get(storeKey)
        return [id, typeof value === 'string' && value.startsWith(ENCRYPTED_PREFIX)]
      }),
    )
  }

  function setCredential(id, value) {
    const { storeKey } = SECRET_FIELDS[assertCredentialId(id)]
    const plaintext = assertCredentialValue(value)
    if (!plaintext) {
      store.delete(storeKey)
      deleteLegacyValue(storeKey)
      return
    }
    store.set(storeKey, encrypt(plaintext))
    deleteLegacyValue(storeKey)
  }

  function deleteCredential(id) {
    const { storeKey } = SECRET_FIELDS[assertCredentialId(id)]
    store.delete(storeKey)
    deleteLegacyValue(storeKey)
  }

  function backendPayload() {
    const payload = {}
    for (const { storeKey, backendField } of Object.values(SECRET_FIELDS)) {
      const storedValue = store.get(storeKey)
      if (typeof storedValue === 'string' && storedValue.startsWith(ENCRYPTED_PREFIX)) {
        payload[backendField] = decrypt(storedValue)
      }
    }
    return payload
  }

  return {
    migrate,
    status,
    setCredential,
    deleteCredential,
    backendPayload,
  }
}

module.exports = {
  ENCRYPTED_PREFIX,
  SECRET_FIELDS,
  createCredentialVault,
}
