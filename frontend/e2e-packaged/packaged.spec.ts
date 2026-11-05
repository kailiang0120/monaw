import { test, expect, _electron, chromium, type Browser, type ElectronApplication, type Page } from '@playwright/test'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { createServer, type Server } from 'node:net'
import { createServer as createHttpServer } from 'node:http'
import { execFileSync, spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'

// Drives the packaged app (release/win-unpacked by default, or MONAW_PACKAGED_APP)
// with no development tools on PATH. The model is never called: a frozen
// backend cannot load the source workflows' Python provider fixture, so these
// checks cover installation-level behavior. See docs/packaging.md.

const frontend = resolve(import.meta.dirname, '..')
const appExe = process.env.MONAW_PACKAGED_APP || join(frontend, 'release', 'win-unpacked', 'Monaw.exe')
const appDir = dirname(appExe)
const windows = process.env.SystemRoot || 'C:\\Windows'

test.skip(!existsSync(appExe), `Packaged app not found at ${appExe}; run scripts/package-windows.ps1 -Unpacked`)

async function freePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done))
  const { port } = server.address() as { port: number }
  await new Promise((done) => server.close(done))
  return port
}

function packagedEnv(home: string, port: number, extra: Record<string, string> = {}): Record<string, string> {
  return {
    SystemRoot: windows,
    windir: windows,
    PATH: [join(windows, 'System32'), windows, join(windows, 'System32', 'WindowsPowerShell', 'v1.0')].join(';'),
    TEMP: join(home, 'temp'),
    TMP: join(home, 'temp'),
    USERPROFILE: home,
    // Windows resolves known folders under USERPROFILE, so the app's AppData (and the
    // legacy key migration that reads and deletes keys there) stays in the test home.
    APPDATA: join(home, 'AppData', 'Roaming'),
    LOCALAPPDATA: join(home, 'AppData', 'Local'),
    MONAW_HOME: home,
    MONAW_BACKEND_PORT: String(port),
    ...extra,
  }
}

/** Monaw processes started from this package, so a test never touches another Monaw. */
function packagedProcesses(name = ''): string[] {
  const filter = name ? `Name='${name}'` : "Name='Monaw.exe' OR Name='monaw-backend.exe'"
  const output = execFileSync('powershell', ['-NoProfile', '-Command',
    `Get-CimInstance Win32_Process -Filter "${filter}" | ForEach-Object { $_.ExecutablePath }`], { encoding: 'utf8' })
  return output.split(/\r?\n/).filter((path) => path && path.toLowerCase().startsWith(appDir.toLowerCase()))
}

/** Startup failures are recorded in the backend log, even when they happen before any output is captured. */
async function startupLog(home: string): Promise<string> {
  return readFile(join(home, 'runtime', 'backend.log'), 'utf8').catch(() => '')
}

async function health(port: number): Promise<boolean> {
  try {
    return (await fetch(`http://127.0.0.1:${port}/health`)).ok
  } catch {
    return false
  }
}

class PackagedApp {
  app!: ElectronApplication
  page!: Page
  output = ''
  constructor(readonly home: string, readonly port: number, readonly extraEnv: Record<string, string> = {}) {}

  async start({ window = true } = {}) {
    this.app = await _electron.launch({ executablePath: appExe, env: packagedEnv(this.home, this.port, this.extraEnv), timeout: 60_000 })
    const capture = (chunk: Buffer) => { this.output = (this.output + chunk.toString()).slice(-20_000) }
    this.app.process().stdout?.on('data', capture)
    this.app.process().stderr?.on('data', capture)
    if (window) this.page = await this.app.firstWindow()
  }

  /** An authenticated call from the renderer, as the app makes it. */
  async api(path: string, body?: unknown, method = body === undefined ? 'GET' : 'POST') {
    return this.page.evaluate(async ({ path, body, method }) => {
      const session = await window.electronAPI!.getControlSession()
      const response = await fetch(window.electronAPI!.backendBaseUrl + path, {
        method,
        headers: { Authorization: `Bearer ${session.token}`, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      return { status: response.status, data: await response.json() }
    }, { path, body, method })
  }

  async quit() {
    await this.app.close().catch(() => this.kill())
    await expect.poll(() => packagedProcesses().length, { timeout: 20_000 }).toBe(0)
    await expect.poll(() => health(this.port), { timeout: 10_000 }).toBe(false)
  }

  kill() {
    const pid = this.app?.process().pid
    if (pid) execFileSync('taskkill', ['/pid', String(pid), '/t', '/f'], { stdio: 'ignore' })
  }
}

let home = ''
let running: PackagedApp[] = []

test.beforeEach(async () => {
  expect(packagedProcesses(), 'another copy of this package is already running').toEqual([])
  // Spaces and non-ASCII characters in the data folder.
  home = await mkdtemp(join(tmpdir(), 'monaw-e2e-packaged Ünïcode 測試 '))
  for (const folder of ['temp', join('AppData', 'Roaming'), join('AppData', 'Local')]) {
    await mkdir(join(home, folder), { recursive: true })
  }
  running = []
})

test.afterEach(async () => {
  for (const app of running) {
    try { app.kill() } catch { /* already gone */ }
  }
  await expect.poll(() => packagedProcesses().length, { timeout: 20_000 }).toBe(0)
  // The installed-update workflow runs on an ephemeral runner; its detached
  // NSIS process can still be releasing files after the app exits.
  if (!process.env.MONAW_UPDATE_TEST_INSTALLER && home.startsWith(tmpdir()) && home.includes('monaw-e2e-packaged')) {
    await rm(home, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 })
  }
})

function track(app: PackagedApp) {
  running.push(app)
  return app
}

test('starts without development tools and guides a first run', async () => {
  const desktop = track(new PackagedApp(home, await freePort()))
  await desktop.start()

  await expect.poll(() => health(desktop.port), { timeout: 30_000 }).toBe(true)
  await expect(desktop.page.getByRole('heading', { name: 'Get started' })).toBeVisible({ timeout: 30_000 })
  await expect(desktop.page.getByText('Connect a model (Needs setup)')).toBeAttached()
  await expect(desktop.page.getByRole('button', { name: 'Run example' })).toBeDisabled()
  expect(packagedProcesses('monaw-backend.exe').length).toBeGreaterThanOrEqual(1)
  expect(existsSync(join(home, 'runtime', 'backend.log'))).toBe(true)

  await desktop.quit()
})

test('shows the installed version and update controls', async () => {
  const desktop = track(new PackagedApp(home, await freePort()))
  await desktop.start()
  await desktop.page.getByRole('button', { name: 'Settings', exact: true }).click()
  await desktop.page.getByRole('button', { name: 'About', exact: true }).click()
  const version = await desktop.app.evaluate(({ app }) => app.getVersion())
  await expect(desktop.page.getByText(version, { exact: true })).toBeVisible()
  await expect(desktop.page.getByRole('button', { name: 'Check for updates' })).toBeEnabled()
  await desktop.quit()
})

for (const corrupted of [false, true]) test(corrupted ? 'rejects an update with the wrong checksum' : 'downloads an update from a controlled test feed', async () => {
  const payload = Buffer.from('Monaw updater test payload; not an installer')
  const checksum = createHash('sha512').update(corrupted ? Buffer.from('different bytes') : payload).digest('base64')
  let payloadRequests = 0
  const server = createHttpServer((request, response) => {
    if (request.url?.split('?')[0] === '/latest.yml') {
      response.writeHead(200, { 'Content-Type': 'text/yaml' })
      response.end(`version: 1.99.0\nfiles:\n  - url: test-update.exe\n    sha512: ${checksum}\n    size: ${payload.length}\npath: test-update.exe\nsha512: ${checksum}\nreleaseDate: '2026-01-01T00:00:00.000Z'\n`)
    } else if (request.url?.split('?')[0] === '/test-update.exe') {
      payloadRequests++
      response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': payload.length })
      response.end(payload)
    } else { response.writeHead(404); response.end() }
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  try {
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('No update test port')
    const desktop = track(new PackagedApp(home, await freePort()))
    await desktop.start()
    // Override the feed only in this isolated test process; production is pinned to GitHub.
    await desktop.app.evaluate((_, url) => {
      const requireFromApp = process.mainModule!.require.bind(process.mainModule)
      const updater = requireFromApp('electron-updater').autoUpdater
      updater.setFeedURL({ provider: 'generic', url })
      updater.disableDifferentialDownload = true
    }, `http://127.0.0.1:${address.port}`)
    await desktop.page.getByRole('button', { name: 'Settings', exact: true }).click()
    await desktop.page.getByRole('button', { name: 'About', exact: true }).click()
    await desktop.page.getByRole('button', { name: 'Check for updates' }).click()
    if (corrupted) {
      await expect(desktop.page.getByText('Could not check for or download the update. Please try again.')).toBeVisible({ timeout: 45_000 })
      await expect(desktop.page.getByRole('button', { name: 'Restart to update' })).toHaveCount(0)
    } else {
      await expect(desktop.page.getByText('Version 1.99.0 is ready to install.')).toBeVisible({ timeout: 45_000 })
      await expect(desktop.page.getByRole('button', { name: 'Restart to update' })).toBeVisible()
    }
    expect(payloadRequests).toBeGreaterThan(0)
    await desktop.quit()
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()))
  }
})

test('installs a newer version and preserves settings', async ({}, testInfo) => {
  const installer = process.env.MONAW_UPDATE_TEST_INSTALLER
  test.skip(!installer, 'Run only on a disposable Windows runner with an installed test build')
  test.setTimeout(180_000)
  const installRoot = resolve(appDir).toLowerCase()
  if (!installRoot.startsWith(resolve(tmpdir()).toLowerCase() + '\\')) throw new Error('Installed-update test must use a temporary install directory')
  const payload = await readFile(installer!)
  const checksum = createHash('sha512').update(payload).digest('base64')
  const server = createHttpServer((request, response) => {
    if (request.url?.split('?')[0] === '/latest.yml') {
      response.writeHead(200, { 'Content-Type': 'text/yaml' })
      response.end(`version: 1.1.1\nfiles:\n  - url: test-update.exe\n    sha512: ${checksum}\n    size: ${payload.length}\npath: test-update.exe\nsha512: ${checksum}\nreleaseDate: '2026-01-01T00:00:00.000Z'\n`)
    } else if (request.url?.split('?')[0] === '/test-update.exe') {
      response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': payload.length })
      response.end(payload)
    } else { response.writeHead(404); response.end() }
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  let appOutput = ''
  let browser: Browser | undefined
  const stopInstalled = () => execFileSync('powershell', ['-NoProfile', '-Command',
    "$p = [IO.Path]::GetFullPath($env:MONAW_PACKAGED_APP); Get-CimInstance Win32_Process -Filter \"Name='Monaw.exe' OR Name='monaw-backend.exe'\" | Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -eq $p -or $_.ExecutablePath.StartsWith([IO.Path]::GetDirectoryName($p) + '\\', [StringComparison]::OrdinalIgnoreCase)) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"], { stdio: 'ignore' })
  try {
    const address = server.address()
    if (!address || typeof address === 'string') throw new Error('No update test port')
    // Configure only this disposable installation; no production feed override.
    await writeFile(join(appDir, 'resources', 'app-update.yml'), `provider: generic\nurl: http://127.0.0.1:${address.port}\nupdaterCacheDirName: monaw-agent-updater\n`)
    const port = await freePort()
    const debugPort = await freePort()
    // CDP attaches to the renderer only. Playwright's Electron main-process
    // inspector prevents normal process exit and can block the NSIS update.
    const child = spawn(appExe, [`--remote-debugging-port=${debugPort}`], {
      env: packagedEnv(home, port), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    })
    const capture = (chunk: Buffer) => { appOutput = (appOutput + chunk.toString()).slice(-20_000) }
    child.stdout?.on('data', capture)
    child.stderr?.on('data', capture)
    await expect.poll(async () => {
      try { browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`); return true } catch { return false }
    }, { timeout: 45_000 }).toBe(true)
    await expect.poll(() => browser?.contexts()[0]?.pages().length || 0, { timeout: 30_000 }).toBeGreaterThan(0)
    const page = browser!.contexts()[0].pages()[0]
    await expect.poll(() => health(port), { timeout: 30_000 }).toBe(true)
    const saveStatus = await page.evaluate(async () => {
      const session = await window.electronAPI!.getControlSession()
      const response = await fetch(window.electronAPI!.backendBaseUrl + '/api/settings', {
        method: 'PUT', headers: { Authorization: `Bearer ${session.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity: { user_name: 'Update test' } }),
      })
      return response.status
    })
    expect(saveStatus).toBe(200)
    await page.getByRole('button', { name: 'Settings', exact: true }).click()
    await page.getByRole('button', { name: 'About', exact: true }).click()
    await page.getByRole('button', { name: 'Check for updates' }).click()
    await expect(page.getByText('Version 1.1.1 is ready to install.')).toBeVisible({ timeout: 60_000 })
    await page.getByRole('button', { name: 'Restart to update' }).click()
    await page.getByRole('button', { name: 'Restart and install' }).click()
    await expect.poll(() => child.exitCode, { timeout: 45_000 }).not.toBeNull()
    await browser?.close().catch(() => {})
    browser = undefined
    await expect.poll(() => {
      try {
        return execFileSync('powershell', ['-NoProfile', '-Command', '(Get-Item -LiteralPath $env:MONAW_PACKAGED_APP).VersionInfo.ProductVersion'], { encoding: 'utf8' }).trim()
      } catch { return '' }
    }, { timeout: 90_000 }).toMatch(/^1\.1\.1/)
    await expect.poll(() => packagedProcesses('Monaw.exe').length, { timeout: 60_000 }).toBeGreaterThan(0)
    // The installer relaunches the app. Stop only this temporary installation,
    // then attach a fresh inspector session and verify persisted data.
    stopInstalled()
    await expect.poll(() => packagedProcesses().length, { timeout: 30_000 }).toBe(0)
    const updated = track(new PackagedApp(home, await freePort()))
    await updated.start()
    expect(await updated.app.evaluate(({ app }) => app.getVersion())).toBe('1.1.1')
    await expect.poll(async () => updated.api('/api/settings').then(result => result.data.identity?.user_name, () => ''), { timeout: 30_000 }).toBe('Update test')
    await updated.quit()
  } finally {
    await browser?.close().catch(() => {})
    await writeFile(testInfo.outputPath('updater-main.log'), appOutput)
    stopInstalled()
    server.closeAllConnections()
    await new Promise<void>((resolve) => server.close(() => resolve()))
  }
})

test('keeps settings across a restart', async () => {
  const port = await freePort()
  const first = track(new PackagedApp(home, port))
  await first.start()
  await first.page.getByRole('button', { name: 'Settings', exact: true }).click()
  await first.page.getByRole('button', { name: 'Identity', exact: true }).click()
  await first.page.getByLabel('Your name').fill('Kai')
  await first.page.getByRole('button', { name: 'Save changes' }).click()
  await expect(first.page.getByText('Settings saved.')).toBeVisible()
  await first.quit()

  const second = track(new PackagedApp(home, port))
  await second.start()
  await second.page.getByRole('button', { name: 'Settings', exact: true }).click()
  await second.page.getByRole('button', { name: 'Identity', exact: true }).click()
  await expect(second.page.getByLabel('Your name')).toHaveValue('Kai')
  await second.quit()
})

test('reports ChatGPT sign-in status without development tools', async () => {
  // The account status must come from the frozen backend, not an error page.
  const desktop = track(new PackagedApp(home, await freePort()))
  await desktop.start()
  const status = desktop.page.waitForResponse((response) => response.url().endsWith('/api/openai-account/status'), { timeout: 60_000 })
  await desktop.page.getByRole('button', { name: 'Settings', exact: true }).click()
  await desktop.page.getByRole('button', { name: 'Connections', exact: true }).click()
  const response = await status
  expect(response.status()).toBe(200)
  expect(await response.json()).toMatchObject({ connected: false })
  await expect(desktop.page.getByText('OpenAI account status unavailable.')).toHaveCount(0)
  await desktop.quit()
})

test('a second launch leaves the running app and its single backend alone', async () => {
  const desktop = track(new PackagedApp(home, await freePort()))
  await desktop.start()
  await expect.poll(() => health(desktop.port), { timeout: 30_000 }).toBe(true)
  const backends = packagedProcesses('monaw-backend.exe').length

  const second = spawn(appExe, [], { env: packagedEnv(home, desktop.port), stdio: 'ignore' })
  const code = await new Promise<number | null>((done) => second.on('exit', done))

  expect(code).toBe(0)
  expect(packagedProcesses('monaw-backend.exe').length).toBe(backends)
  expect(await health(desktop.port)).toBe(true)
  await desktop.quit()
})

test('reports an occupied port and leaves the program using it running', async () => {
  const port = await freePort()
  const other: Server = createServer((socket) => socket.end('other program\n'))
  await new Promise<void>((done) => other.listen(port, '127.0.0.1', done))
  try {
    const desktop = track(new PackagedApp(home, port))
    await desktop.start({ window: false })

    await expect.poll(() => startupLog(home), { timeout: 20_000 }).toContain('"kind":"port_in_use"')
    expect(packagedProcesses('monaw-backend.exe')).toEqual([])
    expect(other.listening).toBe(true)
    desktop.kill()
  } finally {
    await new Promise((done) => other.close(done))
  }
})

test('reports a backend that stops while starting', async () => {
  // A runtime folder under a file cannot be created, so the backend exits during startup.
  await writeFile(join(home, 'blocker'), 'not a folder')
  const desktop = track(new PackagedApp(home, await freePort(), { AGENT_RUNTIME_DIR: join(home, 'blocker', 'runtime') }))
  await desktop.start({ window: false })

  await expect.poll(() => startupLog(home), { timeout: 45_000 }).toMatch(/"kind":"exited","code":\d+/)
  expect(await health(desktop.port)).toBe(false)
  desktop.kill()
})

test.describe('with a ChatGPT account', () => {
  // A release check, run by a person: it opens the ChatGPT sign-in page and
  // waits for them to sign in, then has the model use a tool. See docs/packaging.md.
  test.skip(!process.env.MONAW_LIVE_CHATGPT, 'Set MONAW_LIVE_CHATGPT=1 to sign in and call the model')

  test('signs in and completes a model-driven tool task', async () => {
    test.setTimeout(20 * 60_000)
    const code = `release-${randomUUID().slice(0, 8)}`
    await mkdir(join(home, 'runtime'), { recursive: true })
    await mkdir(join(home, 'workspace'), { recursive: true })
    await writeFile(join(home, 'workspace', 'release-check.txt'), `The release check code is ${code}.\n`)
    await writeFile(join(home, 'runtime', 'settings.json'), JSON.stringify({
      llm: { provider: 'codex' }, memory: { enabled: false, auto_learn: false },
    }))
    const desktop = track(new PackagedApp(home, await freePort()))
    await desktop.start()
    await expect.poll(() => health(desktop.port), { timeout: 30_000 }).toBe(true)

    const login = await desktop.api('/api/openai-account/login', {})
    expect(login.status).toBe(200)
    await desktop.app.evaluate(({ shell }, url) => shell.openExternal(url), login.data.auth_url as string)
    console.log('Sign in with ChatGPT in the browser window that just opened. Waiting up to 10 minutes.')
    await expect.poll(async () => (await desktop.api('/api/openai-account/status')).data.connected, {
      timeout: 10 * 60_000, intervals: [2_000],
    }).toBe(true)

    await desktop.page.getByRole('textbox', { name: 'Message', exact: true })
      .fill('Use a tool to read release-check.txt in the workspace, then reply with only the code it contains.')
    await desktop.page.getByRole('button', { name: 'Send message', exact: true }).click()
    await expect.poll(async () => (await desktop.api('/api/chat/runs')).data.length, { timeout: 30_000 }).toBe(1)
    await expect.poll(async () => (await desktop.api('/api/chat/runs')).data.length, { timeout: 5 * 60_000 }).toBe(0)

    const conversations = await desktop.api('/api/conversations')
    expect(conversations.data).toHaveLength(1)
    const messages = await desktop.api(`/api/conversations/${conversations.data[0].id}/messages`)
    const answer = messages.data.messages.findLast((message: { role: string }) => message.role === 'assistant')
    expect(answer.content).toContain(code)
    expect(answer.tool_calls.length).toBeGreaterThan(0)
    await expect(desktop.page.getByText(code).last()).toBeVisible()
    await desktop.quit()
  })
})
