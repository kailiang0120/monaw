const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const {
  generateControlSecret,
  normalizeBackendHost,
} = require('./control-auth')

const mainSource = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8')
const preloadSource = fs.readFileSync(path.join(__dirname, 'preload.js'), 'utf8')

test('production window runs the renderer in a sandbox', () => {
  assert.match(mainSource, /contextIsolation:\s*true/)
  assert.match(mainSource, /nodeIntegration:\s*false/)
  assert.match(mainSource, /sandbox:\s*true/)
})

test('content security policy has production hard-deny directives', () => {
  assert.match(mainSource, /"object-src 'none'"/)
  assert.match(mainSource, /"base-uri 'none'"/)
  assert.match(mainSource, /"frame-ancestors 'none'"/)
  assert.match(mainSource, /"form-action 'none'"/)
})

test('navigation and IPC paths validate untrusted input', () => {
  assert.match(mainSource, /will-navigate/)
  assert.match(mainSource, /will-frame-navigate/)
  assert.match(mainSource, /will-redirect/)
  assert.match(mainSource, /setWindowOpenHandler/)
  assert.match(mainSource, /assertCredentialId/)
  assert.match(mainSource, /assertTheme/)
  assert.match(mainSource, /assertOptionalIpcString/)
})

test('preload API is frozen and validates arguments', () => {
  assert.match(preloadSource, /Object\.freeze/)
  assert.match(preloadSource, /credentialId\(id\)/)
  assert.match(preloadSource, /safeString\(value,\s*'credential value'\)/)
  assert.match(preloadSource, /optionalString\(defaultPath,\s*'default path'\)/)
  assert.match(preloadSource, /theme\(value\)/)
})

test('backend startup defaults to authenticated loopback control plane', () => {
  assert.equal(normalizeBackendHost('127.0.0.1'), '127.0.0.1')
  assert.equal(normalizeBackendHost('localhost'), 'localhost')
  assert.throws(() => normalizeBackendHost('0.0.0.0'), /Refusing non-loopback backend host/)
  assert.ok(generateControlSecret().length >= 32)
  assert.match(mainSource, /MONAW_CONTROL_SECRET:\s*CONTROL_SECRET/)
  assert.match(mainSource, /MONAW_ALLOW_DEVELOPMENT_TOKEN:\s*'0'/)
  assert.match(mainSource, /--host',\s*BACKEND_HOST/)
})
