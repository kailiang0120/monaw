const crypto = require('crypto')
const path = require('path')

const CONTROL_TOKEN_LIFETIME_SECONDS = 300
const CONTROL_TOKEN_ISSUER = 'monaw-electron'
const CONTROL_SCOPES = [
  'agent:run',
  'settings:read',
  'settings:write',
  'approval:resolve',
  'diagnostics:read',
  'diagnostics:control',
  'files:read',
  'files:mutate',
  'runs:read',
  'runs:control',
]

const CONTROL_PROFILE_STORE_KEY = 'control_profile_id'
const CONTROL_PROFILE_PREFIX = 'safe-profile:v1:'
const CONTROL_PROFILE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/

function generateControlSecret() {
  return crypto.randomBytes(32).toString('base64url')
}

function generateControlSessionId() {
  return crypto.randomBytes(18).toString('base64url')
}

function generateControlProfileId() {
  return `profile-${crypto.randomBytes(32).toString('base64url')}`
}

function assertControlProfileId(value) {
  if (typeof value !== 'string' || !CONTROL_PROFILE_ID_PATTERN.test(value)) {
    throw new Error('Control profile id is invalid')
  }
  return value
}

function loadOrCreateControlProfileId({
  safeStorage,
  store,
  profileContext,
  platform = process.platform,
  generateId = generateControlProfileId,
}) {
  if (!safeStorage || typeof safeStorage.isEncryptionAvailable !== 'function'
    || typeof safeStorage.encryptString !== 'function' || typeof safeStorage.decryptString !== 'function') {
    throw new Error('OS-backed control profile encryption is unavailable')
  }
  if (typeof store?.get !== 'function' || typeof store?.set !== 'function') {
    throw new Error('Control profile storage is unavailable')
  }
  if (typeof profileContext !== 'string' || !profileContext.trim()) {
    throw new Error('Control profile context is unavailable')
  }
  const boundContext = path.resolve(profileContext)
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error('OS-backed control profile encryption is unavailable')
  }
  if (platform === 'linux') {
    if (typeof safeStorage.getSelectedStorageBackend !== 'function') {
      throw new Error('Linux OS-backed control profile storage cannot be verified')
    }
    const backend = safeStorage.getSelectedStorageBackend()
    if (!['gnome_libsecret', 'kwallet', 'kwallet5', 'kwallet6'].includes(backend)) {
      throw new Error('Linux OS-backed control profile encryption is unavailable')
    }
  } else if (platform !== 'win32' && platform !== 'darwin') {
    throw new Error('OS-backed control profile encryption is unsupported on this platform')
  }

  const decryptStored = (stored) => {
    if (typeof stored !== 'string' || !stored.startsWith(CONTROL_PROFILE_PREFIX)) {
      throw new Error('Stored control profile identity is not encrypted')
    }
    let decoded
    try {
      decoded = Buffer.from(stored.slice(CONTROL_PROFILE_PREFIX.length), 'base64')
    } catch {
      throw new Error('Stored control profile identity is invalid')
    }
    let decodedPayload
    try {
      decodedPayload = JSON.parse(safeStorage.decryptString(decoded))
    } catch {
      throw new Error('Stored control profile identity cannot be decrypted')
    }
    if (!decodedPayload || typeof decodedPayload !== 'object'
      || decodedPayload.profile_context !== boundContext) {
      throw new Error('Stored control profile identity belongs to a different Monaw Home')
    }
    return assertControlProfileId(decodedPayload.profile_id)
  }

  const existing = store.get(CONTROL_PROFILE_STORE_KEY)
  if (existing !== undefined && existing !== null && existing !== '') {
    return decryptStored(existing)
  }

  const profileId = assertControlProfileId(generateId())
  const encrypted = safeStorage.encryptString(JSON.stringify({
    profile_id: profileId,
    profile_context: boundContext,
  }))
  if (!Buffer.isBuffer(encrypted) && !(encrypted instanceof Uint8Array)) {
    throw new Error('OS-backed control profile encryption returned invalid data')
  }
  const stored = `${CONTROL_PROFILE_PREFIX}${Buffer.from(encrypted).toString('base64')}`
  store.set(CONTROL_PROFILE_STORE_KEY, stored)
  if (decryptStored(store.get(CONTROL_PROFILE_STORE_KEY)) !== profileId) {
    throw new Error('Control profile identity persistence could not be verified')
  }
  return profileId
}

function mintControlSession(
  secret,
  sessionId,
  nowSeconds = Math.floor(Date.now() / 1000),
  scopes = CONTROL_SCOPES,
  profileId = null,
) {
  if (typeof secret !== 'string' || secret.length < 32) {
    throw new Error('Control-plane secret is not configured')
  }
  if (typeof sessionId !== 'string' || !sessionId) {
    throw new Error('Control-plane session id is required')
  }
  if (profileId !== null && profileId !== undefined) assertControlProfileId(profileId)
  const payload = {
    exp: nowSeconds + CONTROL_TOKEN_LIFETIME_SECONDS,
    iat: nowSeconds,
    iss: CONTROL_TOKEN_ISSUER,
    // The backend binds approval tickets and "this session" grants to this id,
    // so it stays fixed across token refreshes for the app's lifetime.
    jti: sessionId,
    scopes: [...scopes].sort(),
  }
  if (profileId !== null && profileId !== undefined) payload.profile_id = profileId
  const encodedPayload = Buffer.from(JSON.stringify(payload)).toString('base64url')
  const signature = crypto
    .createHmac('sha256', secret)
    .update(encodedPayload, 'ascii')
    .digest('base64url')
  return {
    token: `${encodedPayload}.${signature}`,
    expiresAt: payload.exp * 1000,
  }
}

function normalizeBackendHost(value, allowUnsafe = false) {
  const host = String(value || '127.0.0.1').trim().toLowerCase()
  if (['127.0.0.1', 'localhost', '::1'].includes(host)) return host
  if (allowUnsafe) return host
  throw new Error(`Refusing non-loopback backend host: ${host}`)
}

module.exports = {
  CONTROL_SCOPES,
  CONTROL_PROFILE_PREFIX,
  CONTROL_PROFILE_STORE_KEY,
  generateControlSecret,
  generateControlSessionId,
  generateControlProfileId,
  loadOrCreateControlProfileId,
  mintControlSession,
  normalizeBackendHost,
}
