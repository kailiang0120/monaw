import { test, expect, _electron, type ElectronApplication, type Page } from '@playwright/test'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { createServer, type Server } from 'node:net'
import { execFileSync, spawn } from 'node:child_process'

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
  if (home.startsWith(tmpdir()) && home.includes('monaw-e2e-packaged')) await rm(home, { recursive: true, force: true })
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
