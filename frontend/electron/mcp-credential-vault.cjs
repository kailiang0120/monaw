const crypto = require('node:crypto')
const NAMESPACE = 'mcp_credentials_v1'

function bindingOf(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)
    || Object.keys(input).sort().join(',') !== 'key,server_id,target,transport'
    || !/^[a-f0-9]{32}$/.test(input.server_id)
    || !['stdio', 'streamable_http'].includes(input.transport)
    || input.target !== (input.transport === 'stdio' ? 'env' : 'headers')
    || typeof input.key !== 'string' || !/^[A-Za-z_][A-Za-z0-9_.-]{0,127}$/.test(input.key)) {
    throw new Error('Invalid MCP credential binding')
  }
  return { server_id: input.server_id, transport: input.transport, target: input.target, key: input.key }
}

function referenceOf(input) {
  const b = bindingOf(input)
  return 'mcp:v1:' + crypto.createHash('sha256')
    .update(JSON.stringify([b.server_id, b.transport, b.target, b.key])).digest('hex')
}

function credentialValue(value) {
  if (typeof value !== 'string' || !value || value.length > 20000 || value.includes('\0')
    || ['[redacted]', '[stored]', '********', '••••••••'].includes(value.trim().toLowerCase())) {
    throw new Error('Enter a nonempty credential; masked placeholders are not accepted')
  }
  return value
}

function createMcpCredentialVault({ safeStorage, store }) {
  function requireEncryption() {
    if (!safeStorage.isEncryptionAvailable() || safeStorage.getSelectedStorageBackend?.() === 'basic_text') {
      throw new Error('OS-backed MCP credential encryption is unavailable')
    }
  }
  function records() { return store.get(NAMESPACE, {}) || {} }
  function decrypt(record) {
    requireEncryption()
    if (!record || record.reference !== referenceOf(record.binding) || !record.encrypted?.startsWith('safe:v1:')) {
      throw new Error('Invalid encrypted MCP credential record')
    }
    return credentialValue(safeStorage.decryptString(Buffer.from(record.encrypted.slice(8), 'base64')))
  }
  function prepare(binding, value, legacyDigest = null) {
    requireEncryption()
    const b = bindingOf(binding)
    const reference = referenceOf(b)
    const encrypted = 'safe:v1:' + Buffer.from(safeStorage.encryptString(credentialValue(value))).toString('base64')
    const record = { reference, binding: b, encrypted, state: 'prepared', legacy_digest: legacyDigest }
    if (decrypt(record) !== value) throw new Error('MCP credential encryption verification failed')
    store.set(NAMESPACE, { ...records(), [reference]: record })
    // Read through the durable store before allowing plaintext removal.
    if (decrypt(records()[reference]) !== value) throw new Error('MCP credential storage verification failed')
    return record
  }
  function activate(reference) {
    const all = records()
    if (!all[reference]) throw new Error('Encrypted MCP credential is unavailable')
    decrypt(all[reference])
    all[reference] = { ...all[reference], state: 'active', legacy_digest: null }
    store.set(NAMESPACE, all)
  }
  function remove(binding) {
    const all = records()
    delete all[referenceOf(binding)]
    store.set(NAMESPACE, all)
  }
  return { records, decrypt, prepare, activate, remove }
}

// Serialize migration, hydration and rotation so concurrent IPC requests cannot
// restore an older snapshot or overwrite another connection's credential.
function createMcpCredentialService({ vault, request }) {
  let pending = Promise.resolve()
  function serial(work) {
    const next = pending.then(work, work)
    pending = next.catch(() => {})
    return next
  }
  const inventory = () => request('GET', '/inventory')
  function serverFor(snapshot, binding) {
    const server = snapshot.servers.find(s => s.server_id === binding.server_id && s.transport === binding.transport)
    if (!server) throw new Error('Save the server configuration before changing its credentials')
    return server
  }
  async function apply() {
    let snapshot = await inventory()
    for (const record of Object.values(vault.records())) {
      const binding = bindingOf(record.binding)
      const reference = referenceOf(binding)
      const server = snapshot.servers.find(s => s.server_id === binding.server_id && s.transport === binding.transport)
      if (!server) {
        if (record.state === 'prepared') throw new Error('Prepared MCP migration requires its original saved server binding')
        vault.remove(binding)
        continue
      }
      const bound = server.credential_refs[binding.target]?.[binding.key] === reference
      if (record.state === 'prepared' && !bound) {
        // Recover the exact previously selected field after any interruption.
        // The digest rejects changed legacy plaintext; no plaintext journal exists.
        vault.decrypt(record)
        await request('POST', '/commit', { binding, expected_version: snapshot.settings_version, legacy_digest: record.legacy_digest })
        snapshot = await inventory()
      }
      if (record.state === 'prepared') vault.activate(reference)
    }
    const values = []
    try {
      const records = vault.records()
      for (const server of snapshot.servers) {
        for (const [target, fields] of Object.entries(server.credential_refs)) {
          for (const [key, reference] of Object.entries(fields)) {
            const binding = bindingOf({ server_id: server.server_id, transport: server.transport, target, key })
            if (reference !== referenceOf(binding)) throw new Error('MCP credential reference was rebound')
            if (records[reference]) values.push({ binding, value: vault.decrypt(records[reference]) })
          }
        }
      }
      await request('POST', '/hydrate', { values })
    } finally {
      for (const entry of values) entry.value = ''
    }
    return { ok: true }
  }
  async function operate(input) {
    if (!input || !['migrate', 'replace', 'delete'].includes(input.action)) throw new Error('Invalid MCP credential operation')
    const binding = bindingOf(input.binding)
    const snapshot = await inventory()
    const server = serverFor(snapshot, binding)
    const reference = referenceOf(binding)
    if (input.action === 'delete') {
      if (server.credential_refs[binding.target]?.[binding.key] !== reference) throw new Error('No stored credential at this binding')
      vault.remove(binding)
      await request('POST', '/revoke', { binding })
      // The required slot remains in settings: deletion must not silently start
      // an unauthenticated connection. Re-enter a value or remove the server.
    } else if (input.action === 'migrate') {
      const prepared = await request('POST', '/prepare', { binding, expected_version: snapshot.settings_version })
      try { vault.prepare(binding, prepared.value, prepared.legacy_digest) }
      finally { prepared.value = '' }
    } else {
      if (server.legacy_fields[binding.target]?.includes(binding.key)) throw new Error('Migrate the legacy field before replacing it')
      vault.prepare(binding, credentialValue(input.value))
    }
    return apply()
  }
  return { apply: () => serial(apply), operate: input => serial(() => operate(input)) }
}

module.exports = { bindingOf, referenceOf, credentialValue, createMcpCredentialVault, createMcpCredentialService }
