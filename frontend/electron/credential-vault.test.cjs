const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')

const { ENCRYPTED_PREFIX, createCredentialVault } = require('./credential-vault')

function harness({ initial = {}, legacyData = {}, encryptionAvailable = true } = {}) {
  const values = new Map(Object.entries(initial))
  const deletedLegacy = []
  const store = {
    get: (key) => values.get(key),
    set: (key, value) => values.set(key, value),
    delete: (key) => values.delete(key),
  }
  const safeStorage = {
    isEncryptionAvailable: () => encryptionAvailable,
    encryptString: (value) => Buffer.from(`encrypted:${value}`, 'utf8'),
    decryptString: (value) => value.toString('utf8').replace(/^encrypted:/, ''),
  }
  const vault = createCredentialVault({
    safeStorage,
    store,
    legacyData,
    deleteLegacyValue: (key) => deletedLegacy.push(key),
  })
  return { deletedLegacy, values, vault }
}

test('migrates plaintext credentials and never returns them in status', () => {
  const { deletedLegacy, values, vault } = harness({
    initial: { openai_api_key: 'plain-openai' },
    legacyData: { telegram_bot_token: 'plain-telegram' },
  })

  vault.migrate()

  assert.match(values.get('openai_api_key'), new RegExp(`^${ENCRYPTED_PREFIX}`))
  assert.match(values.get('telegram_bot_token'), new RegExp(`^${ENCRYPTED_PREFIX}`))
  assert.deepEqual(vault.status(), {
    openai: true,
    deepseek: false,
    google: false,
    tavily: false,
    telegramBot: true,
  })
  assert.ok(deletedLegacy.includes('openai_api_key'))
  assert.ok(deletedLegacy.includes('telegram_bot_token'))
})

test('decrypts credentials only for the backend payload', () => {
  const { vault } = harness()
  vault.setCredential('google', 'google-secret')
  vault.setCredential('telegramBot', 'telegram-secret')

  assert.deepEqual(vault.backendPayload(), {
    google_api_key: 'google-secret',
    telegram_bot_token: 'telegram-secret',
  })
})

test('fails closed when OS encryption is unavailable', () => {
  const { vault } = harness({ encryptionAvailable: false })

  assert.throws(() => vault.setCredential('openai', 'secret'), /unavailable/)
  assert.throws(() => vault.migrate(), /unavailable/)
})

test('rejects unknown credential ids', () => {
  const { vault } = harness()
  assert.throws(() => vault.setCredential('unknown', 'secret'), /Unsupported/)
})

test('preload exposes no generic credential read API', () => {
  const preload = fs.readFileSync(path.join(__dirname, 'preload.js'), 'utf8')
  assert.doesNotMatch(preload, /storeGet|store:get|credentialGet|credentials:get/)
  assert.match(preload, /credentialStatus/)
  assert.match(preload, /setCredential/)
  assert.match(preload, /deleteCredential/)
})
