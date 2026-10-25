const test = require('node:test')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const { CONTROL_SCOPES, mintControlSession } = require('./control-auth')
const { bindingOf, referenceOf, credentialValue, createMcpCredentialVault, createMcpCredentialService } = require('./mcp-credential-vault.cjs')

const first = { server_id: 'a'.repeat(32), transport: 'stdio', target: 'env', key: 'CUSTOM_VALUE' }
const second = { ...first, server_id: 'b'.repeat(32) }
const sentinel = 'fixture-private-credential-without-standard-prefix-90876'

function fixture() {
  const data = {}
  const key = crypto.randomBytes(32)
  const safeStorage = {
    isEncryptionAvailable: () => true,
    encryptString: value => { const cipher = crypto.createCipheriv('aes-256-gcm', key, Buffer.alloc(12)); return Buffer.concat([cipher.update(value), cipher.final(), cipher.getAuthTag()]) },
    decryptString: encrypted => { const cipher = crypto.createDecipheriv('aes-256-gcm', key, Buffer.alloc(12)); cipher.setAuthTag(encrypted.subarray(-16)); return Buffer.concat([cipher.update(encrypted.subarray(0, -16)), cipher.final()]).toString() },
  }
  const store = { get: (key, fallback) => structuredClone(data[key] ?? fallback), set: (key, value) => { data[key] = structuredClone(value) } }
  const vault = createMcpCredentialVault({ safeStorage, store })
  let version = 0
  let failCommit = false
  let loseCommitResponse = false
  const hydrated = []
  const revoked = []
  const servers = [first, second].map((b, index) => ({ server_id: b.server_id, name: `server-${index}`, transport: b.transport, credential_refs: {}, legacy_fields: { env: ['CUSTOM_VALUE'], headers: [] } }))
  async function request(method, path, body) {
    if (path === '/inventory') return structuredClone({ settings_version: String(version), servers })
    if (path === '/prepare') return { value: sentinel, legacy_digest: crypto.createHash('sha256').update(sentinel).digest('hex') }
    if (path === '/commit') {
      if (failCommit) throw new Error('interrupted before settings commit')
      assert.equal(body.expected_version, String(version))
      const server = servers.find(s => s.server_id === body.binding.server_id)
      server.credential_refs[body.binding.target] ||= {}
      server.credential_refs[body.binding.target][body.binding.key] = referenceOf(body.binding)
      server.legacy_fields[body.binding.target] = []
      version++
      if (loseCommitResponse) { loseCommitResponse = false; throw new Error('response lost after settings commit') }
      return { ok: true }
    }
    if (path === '/hydrate') { hydrated.push(structuredClone(body.values)); return { ok: true } }
    if (path === '/revoke') { revoked.push(body.binding); return { ok: true } }
    throw new Error('unexpected request')
  }
  return { data, store, safeStorage, vault, servers, hydrated, revoked,
    service: () => createMcpCredentialService({ vault, request }),
    failCommit: value => { failCommit = value }, loseCommitResponse: () => { loseCommitResponse = true } }
}

test('MCP handoff scope is absent from renderer sessions', () => {
  assert.equal(CONTROL_SCOPES.includes('mcp:credentials'), false)
  const renderer = mintControlSession('s'.repeat(32), 'session')
  assert.equal(JSON.parse(Buffer.from(renderer.token.split('.')[0], 'base64url')).scopes.includes('mcp:credentials'), false)
})

test('binding is scoped by server, transport, target, and key; arbitrary vault keys are rejected', () => {
  assert.notEqual(referenceOf(first), referenceOf(second))
  assert.notEqual(referenceOf(first), referenceOf({ ...first, key: 'OTHER' }))
  for (const bad of [{ ...first, namespace: 'openai' }, { ...first, target: 'headers' }, { ...first, key: '../other' }, { ...first, server_id: 'openai' }]) {
    assert.throws(() => bindingOf(bad))
  }
  for (const bad of ['', '[REDACTED]', '[stored]', '********', '••••••••']) assert.throws(() => credentialValue(bad))
})

test('migration encrypts and verifies before removing plaintext; no plaintext is persisted or returned', async () => {
  const f = fixture()
  const result = await f.service().operate({ action: 'migrate', binding: first })
  assert.deepEqual(result, { ok: true })
  assert.equal(JSON.stringify(f.data).includes(sentinel), false)
  assert.equal(JSON.stringify(result).includes(sentinel), false)
  assert.equal(f.vault.records()[referenceOf(first)].state, 'active')
  assert.deepEqual(f.servers[0].legacy_fields.env, [])
  assert.deepEqual(f.hydrated[0], [{ binding: first, value: sentinel }])
})

test('OS encryption failure retains legacy configuration and creates no reference', async () => {
  const f = fixture()
  f.safeStorage.isEncryptionAvailable = () => false
  await assert.rejects(f.service().operate({ action: 'migrate', binding: first }), /encryption/)
  assert.deepEqual(f.servers[0].legacy_fields.env, ['CUSTOM_VALUE'])
  assert.deepEqual(f.vault.records(), {})
  assert.equal(f.hydrated.length, 0)
})

test('encryption verification and durable-store failure retain legacy plaintext', async () => {
  const f = fixture()
  f.safeStorage.decryptString = () => 'wrong plaintext'
  await assert.rejects(f.service().operate({ action: 'migrate', binding: first }), /verification/)
  assert.deepEqual(f.vault.records(), {})
  const other = fixture()
  other.store.set = () => { throw new Error('disk full') }
  await assert.rejects(other.service().operate({ action: 'migrate', binding: first }), /disk full/)
  assert.deepEqual(other.servers[0].credential_refs, {})
})

test('interruption before settings commit resumes from encrypted progress after restarting service', async () => {
  const f = fixture()
  f.failCommit(true)
  await assert.rejects(f.service().operate({ action: 'migrate', binding: first }))
  assert.equal(f.vault.records()[referenceOf(first)].state, 'prepared')
  assert.equal(JSON.stringify(f.data).includes(sentinel), false)
  assert.deepEqual(f.servers[0].legacy_fields.env, ['CUSTOM_VALUE'])
  f.failCommit(false)
  await f.service().apply()
  assert.equal(f.vault.records()[referenceOf(first)].state, 'active')
  assert.equal(f.hydrated.at(-1)[0].value, sentinel)
})

test('lost commit response reconciles without requesting plaintext again or replacing the reference', async () => {
  const f = fixture()
  f.loseCommitResponse()
  await assert.rejects(f.service().operate({ action: 'migrate', binding: first }))
  assert.deepEqual(f.servers[0].legacy_fields.env, [])
  const reference = f.servers[0].credential_refs.env.CUSTOM_VALUE
  await f.service().apply()
  assert.equal(f.servers[0].credential_refs.env.CUSTOM_VALUE, reference)
  assert.equal(f.vault.records()[reference].state, 'active')
})

test('two servers sharing a key rotate independently and survive rehydration', async () => {
  const f = fixture()
  const service = f.service()
  await Promise.all([service.operate({ action: 'migrate', binding: first }), service.operate({ action: 'migrate', binding: second })])
  await service.operate({ action: 'replace', binding: first, value: 'rotated-private-value' })
  await f.service().apply()
  assert.deepEqual(f.hydrated.at(-1), [{ binding: first, value: 'rotated-private-value' }, { binding: second, value: sentinel }])
  assert.equal(JSON.stringify(f.data).includes('rotated-private-value'), false)
})

test('deletion revokes runtime access and does not remove the required slot', async () => {
  const f = fixture()
  const service = f.service()
  await service.operate({ action: 'migrate', binding: first })
  await service.operate({ action: 'delete', binding: first })
  assert.deepEqual(f.vault.records(), {})
  assert.equal(f.servers[0].credential_refs.env.CUSTOM_VALUE, referenceOf(first))
  assert.deepEqual(f.revoked, [first])
  assert.deepEqual(f.hydrated.at(-1), [])
  await assert.rejects(service.operate({ action: 'replace', binding: { ...first, server_id: 'c'.repeat(32) }, value: sentinel }))
})

test('interrupted migration is not reported migrated when its saved binding changes', async () => {
  const f = fixture()
  f.failCommit(true)
  await assert.rejects(f.service().operate({ action: 'migrate', binding: first }))
  f.failCommit(false)
  f.servers[0].transport = 'streamable_http'
  await assert.rejects(f.service().apply(), /original saved server binding/)
  assert.equal(f.vault.records()[referenceOf(first)].state, 'prepared')
})
