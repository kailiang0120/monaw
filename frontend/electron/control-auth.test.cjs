const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const test = require('node:test')

const {
  CONTROL_SCOPES,
  generateControlSecret,
  mintControlSession,
  normalizeBackendHost,
} = require('./control-auth')

test('mints a short-lived signed renderer session', () => {
  const secret = generateControlSecret()
  const session = mintControlSession(secret, 1_000)
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
})

test('rejects non-loopback backend hosts by default', () => {
  assert.equal(normalizeBackendHost('localhost'), 'localhost')
  assert.throws(() => normalizeBackendHost('0.0.0.0'), /non-loopback/)
  assert.equal(normalizeBackendHost('0.0.0.0', true), '0.0.0.0')
})
