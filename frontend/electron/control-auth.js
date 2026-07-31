const crypto = require('crypto')

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
]

function generateControlSecret() {
  return crypto.randomBytes(32).toString('base64url')
}

function mintControlSession(secret, nowSeconds = Math.floor(Date.now() / 1000)) {
  if (typeof secret !== 'string' || secret.length < 32) {
    throw new Error('Control-plane secret is not configured')
  }
  const payload = {
    exp: nowSeconds + CONTROL_TOKEN_LIFETIME_SECONDS,
    iat: nowSeconds,
    iss: CONTROL_TOKEN_ISSUER,
    jti: crypto.randomBytes(18).toString('base64url'),
    scopes: [...CONTROL_SCOPES].sort(),
  }
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
  generateControlSecret,
  mintControlSession,
  normalizeBackendHost,
}
