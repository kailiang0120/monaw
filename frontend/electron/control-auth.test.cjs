const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const test = require('node:test')

const {
  CONTROL_SCOPES,
  generateControlSecret,
  generateControlSessionId,
  loadOrCreateControlProfileId,
  mintControlSession,
  normalizeBackendHost,
} = require('./control-auth')

test('mints a short-lived signed renderer session', () => {
  const secret = generateControlSecret()
  const session = mintControlSession(secret, 'session-1', 1_000)
  const [encodedPayload, encodedSignature] = session.token.split('.')
  const payload = JSON.parse(Buffer.from(encodedPayload, 'base64url').toString('utf8'))
  const expectedSignature = crypto
    .createHmac('sha256', secret)
    .update(encodedPayload, 'ascii')
    .digest('base64url')

  assert.equal(encodedSignature, expectedSignature)
  assert.equal(payload.exp, 1_300)
  assert.deepEqual(payload.scopes, [...CONTROL_SCOPES].sort())
  assert.equal(session.expiresAt, 1_300_000)
  assert.equal(payload.jti, 'session-1')
})

test('keeps the session id stable across token refreshes', () => {
  const secret = generateControlSecret()
  const sessionId = generateControlSessionId()
  const jti = (session) => JSON.parse(Buffer.from(session.token.split('.')[0], 'base64url').toString('utf8')).jti

  assert.equal(jti(mintControlSession(secret, sessionId, 1_000)), sessionId)
  assert.equal(jti(mintControlSession(secret, sessionId, 2_000)), sessionId)
  assert.throws(() => mintControlSession(secret, ''), /session id is required/)
})

test('persists the profile id only through OS-backed encryption', () => {
  const values = new Map()
  const store = {
    get: key => values.get(key),
    set: (key, value) => values.set(key, value),
  }
  const safeStorage = {
    isEncryptionAvailable: () => true,
    encryptString: value => Buffer.from(`protected:${value}`, 'utf8'),
    decryptString: value => {
      const decoded = value.toString('utf8')
      if (!decoded.startsWith('protected:')) throw new Error('invalid ciphertext')
      return decoded.slice('protected:'.length)
    },
  }
  const profileId = 'profile-stable-1'

  const profileOptions = { safeStorage, store, profileContext: 'monaw-home-a', platform: 'win32' }
  assert.equal(loadOrCreateControlProfileId({ ...profileOptions, generateId: () => profileId }), profileId)
  const stored = values.get('control_profile_id')
  assert.match(stored, /^safe-profile:v1:/)
  assert.equal(stored.includes(profileId), false)
  assert.equal(loadOrCreateControlProfileId({ ...profileOptions, generateId: () => 'profile-new' }), profileId)
  assert.throws(
    () => loadOrCreateControlProfileId({ ...profileOptions, profileContext: 'monaw-home-b' }),
    /different Monaw Home/,
  )

  const token = mintControlSession(generateControlSecret(), generateControlSessionId(), 1_000, undefined, profileId)
  const payload = JSON.parse(Buffer.from(token.token.split('.')[0], 'base64url').toString('utf8'))
  assert.equal(payload.profile_id, profileId)
  assert.ok(payload.scopes.includes('files:mutate'))
  assert.ok(payload.scopes.includes('runs:read'))
  assert.ok(payload.scopes.includes('runs:control'))
})

test('fails closed when profile encryption is unavailable or stored identity is plaintext', () => {
  const values = new Map()
  const store = {
    get: key => values.get(key),
    set: (key, value) => values.set(key, value),
  }
  const unavailableStorage = {
    isEncryptionAvailable: () => false,
    encryptString: value => Buffer.from(value),
    decryptString: value => value.toString('utf8'),
  }
  assert.throws(
    () => loadOrCreateControlProfileId({
      safeStorage: unavailableStorage, store, profileContext: 'monaw-home', platform: 'win32',
    }),
    /encryption is unavailable/,
  )

  values.set('control_profile_id', 'profile-from-agent-file')
  const availableStorage = { ...unavailableStorage, isEncryptionAvailable: () => true }
  assert.throws(
    () => loadOrCreateControlProfileId({
      safeStorage: availableStorage, store, profileContext: 'monaw-home', platform: 'win32',
    }),
    /not encrypted/,
  )
})

test('rejects Linux profile storage backed by basic_text', () => {
  const safeStorage = {
    isEncryptionAvailable: () => true,
    getSelectedStorageBackend: () => 'basic_text',
    encryptString: value => Buffer.from(value),
    decryptString: value => value.toString('utf8'),
  }
  const store = { get: () => undefined, set: () => {} }
  assert.throws(
    () => loadOrCreateControlProfileId({ safeStorage, store, profileContext: 'home', platform: 'linux' }),
    /encryption is unavailable/,
  )
})

test('rejects non-loopback backend hosts by default', () => {
  assert.equal(normalizeBackendHost('localhost'), 'localhost')
  assert.throws(() => normalizeBackendHost('0.0.0.0'), /non-loopback/)
  assert.equal(normalizeBackendHost('0.0.0.0', true), '0.0.0.0')
})
