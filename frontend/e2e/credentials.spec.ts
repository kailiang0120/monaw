import { test, expect } from './desktop'
import { readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { createHash } from 'node:crypto'

test('OS encrypted MCP credentials hydrate, rotate, reconnect after schema change, survive restart, and revoke', async ({ desktop }) => {
  test.setTimeout(120_000)
  const first = 'monaw-fixture-private-initial-8739872'
  const rotated = 'monaw-fixture-private-rotated-3892873'
  const digest = (value: string) => createHash('sha256').update(value).digest('hex')
  const lines = (name: string) => readFile(join(desktop.root, 'control', name), 'utf8')
    .then(text => text.trim().split('\n').filter(Boolean).map(line => JSON.parse(line)), () => [])
  const configured = await desktop.api('/api/settings', { mcp: { enabled: true, servers: [{
    name: 'fixture', enabled: false, command: join(desktop.root, 'venv', 'Scripts', 'python.exe'),
    args: [resolve(import.meta.dirname, 'fixtures/python/mcp_fixture.py'), join(desktop.root, 'control')],
    trusted_tools: ['get_credential_status', 'get_schema_version'], startup_timeout_ms: 15000,
  }] } }, 'PUT')
  expect(configured.status).toBe(200)
  const server = configured.data.mcp.servers[0]
  const binding = { server_id: server.server_id, transport: 'stdio' as const, target: 'env' as const, key: 'API_KEY' }
  const credential = (action: 'migrate' | 'replace' | 'delete', value?: string) => desktop.page.evaluate(
    input => window.electronAPI!.changeMcpCredential(input), { action, binding, value })
  expect((await desktop.api('/api/settings/mcp-credentials/inventory')).status).toBe(403)
  // Seed a legacy file only inside the disposable fixture. No API can create
  // a new secret in ordinary configuration.
  await desktop.close()
  const settingsPath = join(desktop.root, 'runtime', 'settings.json')
  const legacy = JSON.parse(await readFile(settingsPath, 'utf8'))
  legacy.mcp.servers[0].env.API_KEY = first
  await writeFile(settingsPath, JSON.stringify(legacy))
  await desktop.start()
  expect(JSON.stringify((await desktop.api('/api/settings')).data)).not.toContain(first)
  expect(await desktop.app.evaluate(({ safeStorage }) => safeStorage.isEncryptionAvailable())).toBe(true)
  await credential('migrate')
  let saved = (await desktop.api('/api/settings')).data
  expect(JSON.stringify(saved)).not.toContain(first)
  expect(saved.mcp.servers[0].credential_refs.env.API_KEY).toMatch(/^mcp:v1:/)
  saved.mcp.servers[0].enabled = true
  expect((await desktop.api('/api/settings', { mcp: saved.mcp }, 'PUT')).status).toBe(200)
  await expect.poll(async () => (await lines('mcp-starts.jsonl')).at(-1)?.digest).toBe(digest(first))
  await desktop.send('mcp')
  await desktop.page.getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(desktop.page.getByText('Fixture mcp completed.', { exact: false }).last()).toBeVisible()
  await expect.poll(async () => (await lines('mcp-calls.jsonl')).at(-1)?.digest).toBe(digest(first))
  await credential('replace', rotated)
  await expect.poll(async () => (await lines('mcp-starts.jsonl')).at(-1)?.digest).toBe(digest(rotated))
  await desktop.close()
  await desktop.start()
  await expect.poll(async () => (await lines('mcp-starts.jsonl')).length).toBeGreaterThanOrEqual(3)
  expect((await lines('mcp-starts.jsonl')).at(-1)?.digest).toBe(digest(rotated))
  await writeFile(join(desktop.root, 'control', 'mcp-schema-v2'), '')
  const reconnected = await desktop.api('/api/diagnostics/mcp/fixture/reconnect', {})
  expect(reconnected.status).toBe(200)
  expect(reconnected.data.connected).toBe(true)
  expect(reconnected.data.remote_tool_names).toContain('get_schema_version')
  await desktop.send('mcp-schema')
  await desktop.page.getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(desktop.page.getByText('Fixture mcp-schema completed.', { exact: false }).last()).toBeVisible()
  expect((await lines('mcp-calls.jsonl')).at(-1)).toEqual({ digest: digest(rotated), version: 2 })
  expect((await desktop.events()).some(event => event.kind === 'mcp_schema' && event.schema_has_extra)).toBe(true)
  const diagnostics = JSON.stringify((await desktop.api('/api/diagnostics/mcp')).data)
  expect(diagnostics).toContain('[REDACTED]')
  for (const secret of [first, rotated]) {
    expect(diagnostics).not.toContain(secret)
    expect(await readFile(join(desktop.root, 'runtime', 'settings.json'), 'utf8')).not.toContain(secret)
    expect(await readFile(join(desktop.root, 'runtime', 'backend.log'), 'utf8')).not.toContain(secret)
  }
  // The real Electron store must contain ciphertext, never the entered plaintext.
  const userData = await desktop.app.evaluate(({ app }) => app.getPath('userData'))
  const encryptedStore = await readFile(join(userData, 'config.json'), 'utf8')
  expect(encryptedStore).toContain('safe:v1:')
  expect(encryptedStore).not.toContain(first)
  expect(encryptedStore).not.toContain(rotated)
  await credential('delete')
  const before = (await lines('mcp-starts.jsonl')).length
  const missing = await desktop.api('/api/diagnostics/mcp/fixture/reconnect', {})
  expect(missing.data.connected).toBe(false)
  expect(missing.data.state).toBe('credentials_unavailable')
  expect((await lines('mcp-starts.jsonl')).length).toBe(before)
  expect(desktop.errors).toEqual([])
})
